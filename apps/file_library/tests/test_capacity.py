# ruff: noqa: F811
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier, Event
from time import monotonic

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.assignments.services import assign_tablet_vehicle
from apps.audit.models import AuditEvent
from apps.file_library import capacity, services
from apps.file_library.models import CapacityReview, Content, Entry, StorageImpact
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import crypto, publish  # noqa: F401
from apps.organizations import services as organization_services
from apps.organizations.models import Vehicle
from apps.tablets import services as tablet_services
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.tests.test_management_ui import _adopted_installation

# Real commits are requested explicitly by lifecycle and concurrency tests below.
pytestmark = pytest.mark.django_db


def tablet_for(scope, *, station=None, free=1_000_000, total=1_000_000):
    station = station or scope[5]
    tablet = Tablet.objects.create(
        department=station.department, status="ACTIVE", display_name=str(uuid.uuid4())
    )
    vehicle = Vehicle.objects.create(
        department=station.department, station=station, display_name=str(uuid.uuid4())
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=scope[0]
    )
    installation = _adopted_installation(tablet)
    installation.free_storage_bytes = free
    installation.total_storage_bytes = total
    installation.storage_observed_at = timezone.now()
    installation.save(
        update_fields=(
            "free_storage_bytes",
            "total_storage_bytes",
            "storage_observed_at",
        )
    )
    return tablet, installation


def operation(*, size=100, document_id=None, sha256="a" * 64):
    return {
        "kind": "upload",
        "document_id": str(document_id or uuid.uuid4()),
        "replace_id": None,
        "parent_id": None,
        "name": "guide.pdf",
        "size_bytes": size,
        "sha256": sha256,
    }


def projection(library, proposed):
    with transaction.atomic():
        capacity.lock_capacity(library)
        return capacity.project(library, proposed)


def upload_bytes(data, name="guide.pdf"):
    return SimpleUploadedFile(name, data, content_type="application/pdf")


def warning(actor, library, data):
    with pytest.raises(services.CapacityWarning) as error:
        services.upload_pdf(actor=actor, library_id=library.id, upload=upload_bytes(data))
    return error.value.review


def approve(actor, review, **kwargs):
    return capacity.decide(
        actor=actor,
        review_id=review.id,
        confirm=True,
        reauthenticated_at=timezone.now(),
        expected_fingerprint=review.fingerprint,
        **kwargs,
    )


@pytest.mark.parametrize("free,state", [(316, "ok"), (315, "warning")])
def test_exact_twenty_percent_and_gcm_tag(scope, free, state):
    library = library_for(scope)
    _tablet, _installation = tablet_for(scope, free=free, total=1000)
    result = projection(library, operation())["tablets"][0]
    assert result["incremental_ciphertext_bytes"] == 116
    assert result["projected_free_bytes"] == free - 116
    assert result["state"] == state


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "STALE"}, "stale_installation"),
        (
            {"authorization_valid_until": timezone.now() - timedelta(seconds=1)},
            "stale_installation",
        ),
        (
            {"storage_observed_at": timezone.now() - timedelta(days=2)},
            "missing_or_outdated_telemetry",
        ),
        (
            {"free_storage_bytes": None, "total_storage_bytes": None, "storage_observed_at": None},
            "missing_or_outdated_telemetry",
        ),
    ],
)
def test_unknown_telemetry(scope, change, reason):
    library = library_for(scope)
    _tablet, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(**change)
    result = projection(library, operation())["tablets"][0]
    assert result["state"] == "unknown"
    assert result["projected_free_bytes"] is None
    assert result["reason"] == reason


def test_assignment_after_measurement_requires_fresh_check_in(scope):
    library = library_for(scope)
    tablet, installation = tablet_for(scope)
    TabletVehicleAssignment.objects.filter(tablet=tablet).update(
        valid_from=installation.storage_observed_at + timedelta(microseconds=1)
    )
    result = projection(library, operation())["tablets"][0]
    assert result["state"] == "unknown"
    assert result["reason"] == "assignment_changed_since_telemetry"
    AppInstallation.objects.filter(pk=installation.pk).update(storage_observed_at=timezone.now())
    assert projection(library, operation())["tablets"][0]["state"] == "ok"


@pytest.mark.parametrize("future", [True, False])
def test_only_current_operational_assignment_windows_are_affected(scope, future):
    library = library_for(scope)
    tablet, _installation = tablet_for(scope)
    now = timezone.now()
    TabletVehicleAssignment.objects.filter(tablet=tablet).update(
        valid_from=now + timedelta(days=1) if future else now - timedelta(hours=1),
        valid_until=None if future else now - timedelta(minutes=1),
    )
    assert projection(library, operation())["tablets"] == []


def test_scope_isolation_and_missing_replacement_measurement(scope):
    library = services.get_library(scope[1], station_id=scope[5].id)
    own, installation = tablet_for(scope)
    tablet_for(scope, station=scope[6])
    replacement = _adopted_installation
    AppInstallation.objects.filter(pk=installation.pk).update(status="REPLACED")
    new = replacement(own)
    result = projection(library, operation())["tablets"]
    assert [row["tablet_id"] for row in result] == [str(own.id)]
    assert result[0]["installation_id"] == str(new.id)
    assert result[0]["state"] == "unknown"
    department_library = library_for(scope)
    assert len(projection(department_library, operation())["tablets"]) == 2


def test_published_artifact_reuse_and_document_identity(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    document = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    publish(scope[0], library)
    _tablet, installation = tablet_for(scope)
    proposed = operation(
        size=document.content.size_bytes, document_id=document.id, sha256=document.content.sha256
    )
    first = projection(library, proposed)["tablets"][0]
    assert first["incremental_ciphertext_bytes"] == 0
    StorageImpact.objects.create(
        library=library,
        document_id=document.id,
        sha256=document.content.sha256,
        size_bytes=document.content.size_bytes,
    )
    assert projection(library, proposed)["tablets"][0]["incremental_ciphertext_bytes"] == 0
    proposed["document_id"] = str(uuid.uuid4())
    second = projection(library, proposed)["tablets"][0]
    assert second["incremental_ciphertext_bytes"] == document.content.size_bytes + 16
    installation.refresh_from_db()
    assert installation.free_storage_bytes == 1_000_000


def test_override_is_specific_atomic_single_use_and_audited(scope, pdf_pipeline):
    library = library_for(scope)
    _tablet, installation = tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[0], library, data)
    library.refresh_from_db()
    assert library.usage_bytes == library.source_revision == 0
    assert not Entry.objects.exists() and not Content.objects.exists()
    assert not list((pdf_pipeline / "accepted").rglob("*.pdf"))
    approve(scope[0], review)
    document = services.upload_pdf(
        actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
    )
    assert str(document.id) == review.operation["document_id"]
    review.refresh_from_db()
    assert review.consumed_at is not None
    with pytest.raises(services.LibraryError):
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
        )
    approved_event = AuditEvent.objects.get(
        action="file_library.capacity_override_approved", target_uuid=review.id
    )
    values = approved_event.metadata["projection"]["tablets"][0]
    assert values["free_storage_bytes"] == 10
    assert values["total_storage_bytes"] == 1000
    assert values["observed_at"] == installation.storage_observed_at.isoformat()
    assert values["projected_free_bytes"] == 10 - len(data) - 16
    assert values["reason"] == "projected_low_storage"
    assert approved_event.actor_user_id == scope[0].id
    installation.refresh_from_db()
    assert installation.free_storage_bytes == 10
    assert StorageImpact.objects.count() == 1


@pytest.mark.parametrize("change", ["source", "telemetry", "expiry", "reviewer_authority"])
def test_stale_confirmation_is_rejected(scope, pdf_pipeline, change):
    library = library_for(scope)
    _tablet, installation = tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[0], library, data)
    approve(scope[0], review)
    if change == "source":
        services.create_folder(actor=scope[0], library_id=library.id, name="Changed")
    elif change == "telemetry":
        AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=11)
    elif change == "expiry":
        CapacityReview.objects.filter(pk=review.pk).update(expires_at=timezone.now())
    else:
        from apps.authorization.models import DepartmentMembership

        DepartmentMembership.objects.filter(user=scope[0]).update(status="SUSPENDED")
    with pytest.raises((services.LibraryError, PermissionDenied)):
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
        )
    assert not Entry.objects.filter(is_folder=False).exists()
    review.refresh_from_db()
    assert review.consumed_at is None


def test_station_escalation_and_temporary_permission_boundaries(scope, pdf_pipeline, client):
    library = services.get_library(scope[1], station_id=scope[5].id)
    tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[1], library, data)
    client.force_login(scope[2])
    assert client.get(reverse("file-library-capacity-review", args=(review.id,))).status_code == 403
    for actor in (scope[1], scope[2]):
        with pytest.raises(PermissionDenied):
            approve(actor, review)
    services.activate_temporary_access(actor=scope[0], station=scope[5])
    with pytest.raises(PermissionDenied):
        approve(scope[0], review)
    services.revoke_temporary_access(actor=scope[0])
    url = reverse("file-library-capacity-review", args=(review.id,))
    client.force_login(scope[0])
    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {
            "action": "approve",
            "confirm": "yes",
            "fingerprint": review.fingerprint,
        },
    )
    assert response.status_code == 302 and "reauthenticate" in response.url
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    assert (
        client.post(
            url,
            {
                "action": "approve",
                "confirm": "yes",
                "fingerprint": review.fingerprint,
            },
        ).status_code
        == 302
    )
    review.refresh_from_db()
    assert review.decision == "approved"
    assert client.post(url, {"action": "retry", "pdf": upload_bytes(data)}).status_code == 403
    client.force_login(scope[1])
    assert client.post(url, {"action": "approve", "confirm": "yes"}).status_code == 403
    assert client.post(url, {"action": "retry", "pdf": upload_bytes(data)}).status_code == 302
    assert Entry.objects.filter(library=library, is_folder=False).count() == 1


def test_approval_requires_confirmation_recent_reauth_and_review_fingerprint(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=10, total=1000)
    review = warning(scope[0], library, pdf().read())
    for confirm, when in (
        (False, timezone.now()),
        ("yes", timezone.now()),
        (True, None),
        (True, timezone.now() - timedelta(hours=1)),
    ):
        with pytest.raises(PermissionDenied):
            capacity.decide(
                actor=scope[0],
                review_id=review.id,
                confirm=confirm,
                reauthenticated_at=when,
                expected_fingerprint=review.fingerprint,
            )
    with pytest.raises(services.LibraryError):
        capacity.decide(
            actor=scope[0],
            review_id=review.id,
            confirm=True,
            reauthenticated_at=timezone.now(),
            expected_fingerprint="stale",
        )


@pytest.mark.django_db(transaction=True)
def test_cross_scope_concurrent_uploads_share_reservations(scope, pdf_pipeline):
    department_library = library_for(scope)
    station_library = services.get_library(scope[1], station_id=scope[5].id)
    data = pdf().read()
    charge = len(data) + 16
    tablet_for(scope, free=2000 + charge + charge // 2, total=10000)
    barrier = Barrier(2)

    def upload(actor, library):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            services.upload_pdf(actor=actor, library_id=library.id, upload=upload_bytes(data))
            return "accepted"
        except services.CapacityWarning:
            return "warning"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(upload, scope[0], department_library),
            executor.submit(upload, scope[1], station_library),
        )
        assert sorted(f.result(timeout=30) for f in futures) == ["accepted", "warning"]
    assert Entry.objects.filter(is_folder=False).count() == StorageImpact.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_failure_after_override_consumption_rolls_back(scope, pdf_pipeline, monkeypatch):
    library = library_for(scope)
    tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[0], library, data)
    approve(scope[0], review)
    monkeypatch.setattr(
        services, "changed", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rollback"))
    )
    with pytest.raises(RuntimeError, match="rollback"):
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
        )
    review.refresh_from_db()
    assert review.consumed_at is None
    assert not Entry.objects.exists() and not StorageImpact.objects.exists()
    assert not Content.objects.exists()
    assert not AuditEvent.objects.filter(action="file_library.capacity_override_consumed").exists()


def test_projection_queries_are_bounded_and_other_departments_are_excluded(scope):
    library = library_for(scope)
    tablet_for(scope)
    candidate = operation()
    with CaptureQueriesContext(connection) as first:
        projection(library, candidate)
    for _ in range(5):
        tablet_for(scope)
    with CaptureQueriesContext(connection) as second:
        result = projection(library, candidate)
    assert len(first) == len(second)
    assert len(result["tablets"]) == 6


def test_metadata_edits_and_unchanged_ciphertext_do_not_require_overrides(scope, pdf_pipeline):
    library = library_for(scope)
    data = pdf().read()
    document = services.upload_pdf(actor=scope[0], library_id=library.id, upload=upload_bytes(data))
    _tablet, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        free_storage_bytes=None, total_storage_bytes=None, storage_observed_at=None
    )
    services.edit_entry(actor=scope[0], library_id=library.id, entry_id=document.id, name="New.pdf")
    services.upload_pdf(
        actor=scope[0], library_id=library.id, replace_id=document.id, upload=upload_bytes(data)
    )
    assert not CapacityReview.objects.exists()
    assert StorageImpact.objects.count() == 1


def test_reducing_source_replacement_reserves_full_new_ciphertext(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    document = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf(pages=2))
    publish(scope[0], library)
    tablet_for(scope, free=10, total=1000)
    smaller = pdf().read()
    with pytest.raises(services.CapacityWarning) as error:
        services.upload_pdf(
            actor=scope[0],
            library_id=library.id,
            replace_id=document.id,
            upload=upload_bytes(smaller),
        )
    result = error.value.review.projection["tablets"][0]
    assert result["incremental_ciphertext_bytes"] == len(smaller) + 16
    assert document.content.size_bytes > len(smaller)


@pytest.mark.django_db(transaction=True)
def test_pruned_history_since_measurement_is_unknown(scope, pdf_pipeline, crypto):
    from apps.publications.retention import run_publication_retention

    library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    tablet_for(scope)
    for index in range(4):
        services.rename_library(actor=scope[0], library_id=library.id, name=f"Name {index}")
        publish(scope[0], library)
    run_publication_retention()
    result = projection(library, operation())["tablets"][0]
    assert result["state"] == "unknown"
    assert result["reason"] == "publication_history_unavailable"


def test_native_deployment_check_fails_closed_on_windows(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from apps.file_library.management.commands import verify_library_pdf_sanitizer

    monkeypatch.setattr(verify_library_pdf_sanitizer.sys, "platform", "win32")
    with pytest.raises(CommandError, match="deployed Linux"):
        call_command("verify_library_pdf_sanitizer")


def test_override_does_not_bypass_quota_or_accept_different_bytes(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[0], library, data)
    approve(scope[0], review)
    with pytest.raises(services.LibraryError, match="stale"):
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=pdf(pages=2), approval_id=review.id
        )
    services.set_library_quota(actor=scope[0], library_id=library.id, quota_bytes=0)
    with pytest.raises(services.LibraryError, match="quota"):
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
        )
    review.refresh_from_db()
    assert review.consumed_at is None
    assert not Entry.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_override_consumption_is_single_use(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=10, total=1000)
    data = pdf().read()
    review = warning(scope[0], library, data)
    approve(scope[0], review)
    barrier = Barrier(2)

    def commit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            services.upload_pdf(
                actor=scope[0],
                library_id=library.id,
                upload=upload_bytes(data),
                approval_id=review.id,
            )
            return "accepted"
        except services.LibraryError:
            return "rejected"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(commit), executor.submit(commit))
        assert sorted(f.result(timeout=30) for f in futures) == ["accepted", "rejected"]
    assert Entry.objects.count() == StorageImpact.objects.count() == 1
    assert AuditEvent.objects.filter(action="file_library.capacity_override_consumed").count() == 1


def serialized_operations(first, second, *, while_blocked=None):
    """Hold the first commit and prove the second waits on its advisory lock.

    Each worker owns a real connection. Observing pg_locks (rather than merely
    starting a thread) establishes that the competing operation reached the DB.
    Timeouts bound failures, including a regression to row-lock inversion.
    """
    assert connection.vendor == "postgresql"
    held, release = Event(), Event()
    second_pid: Queue[int] = Queue()

    def run(action, *, hold=False):
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                if not hold:
                    second_pid.put(pid)
                result = action()
                if hold:
                    held.set()
                    assert release.wait(10), "First transaction was never released"
                return result
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(run, first, hold=True)
        waiting = None
        try:
            assert held.wait(10), "First operation did not complete"
            waiting = pool.submit(run, second)
            pid = second_pid.get(timeout=10)
            deadline = monotonic() + 8
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                        [pid],
                    )
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                if waiting.done():
                    waiting.result()  # Surface errors before reporting missing synchronization.
                    pytest.fail("Competing operation bypassed the capacity lock")
                assert monotonic() < deadline, "No advisory lock wait observed"
                release.wait(0.01)
            if while_blocked:
                while_blocked()
        finally:
            release.set()
        return holding.result(timeout=20), waiting.result(timeout=20)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("movement", ["same_station", "outbound", "inbound"])
def test_projection_serializes_reassignment_between_station_scopes(scope, movement):
    library = services.get_library(scope[1], station_id=scope[5].id)
    source_station = scope[6] if movement == "inbound" else scope[5]
    target_station = scope[6] if movement == "outbound" else scope[5]
    tablet, _ = tablet_for(scope, station=source_station)
    original = TabletVehicleAssignment.objects.get(tablet=tablet)
    target = Vehicle.objects.create(
        department=scope[3], station=target_station, display_name="Target"
    )
    proposed = operation()
    before, assigned = serialized_operations(
        lambda: projection(library, proposed),
        lambda: assign_tablet_vehicle(tablet=tablet, vehicle=target, actor=scope[0]),
    )
    assert bool(before["tablets"]) == (movement != "inbound")
    original.refresh_from_db()
    assert original.ended_at is not None
    assert original.end_reason == "REASSIGNED"
    assert TabletVehicleAssignment.objects.get(tablet=tablet, ended_at=None).pk == assigned.pk
    after = projection(library, proposed)
    assert bool(after["tablets"]) == (movement != "outbound")
    if after["tablets"]:
        assert after["tablets"][0]["assignment_id"] == str(assigned.id)
        assert after["tablets"][0]["reason"] == "assignment_changed_since_telemetry"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "transition,initial,expected,eligible",
    [
        ("activate_tablet", "INACTIVE", "ACTIVE", True),
        ("deactivate_tablet", "ACTIVE", "INACTIVE", False),
        ("mark_tablet_lost", "ACTIVE", "LOST", False),
        ("recover_tablet", "LOST", "INACTIVE", False),
        ("retire_tablet", "ACTIVE", "RETIRED", False),
    ],
)
def test_projection_serializes_tablet_lifecycle(scope, transition, initial, expected, eligible):
    library = library_for(scope)
    tablet, installation = tablet_for(scope)
    Tablet.objects.filter(pk=tablet.pk).update(status=initial, active=initial != "LOST")
    proposed = operation()
    before, _ = serialized_operations(
        lambda: projection(library, proposed),
        lambda: getattr(tablet_services, transition)(actor=scope[0], tablet=tablet),
    )
    assert bool(before["tablets"]) == (initial == "ACTIVE")
    tablet.refresh_from_db()
    assert tablet.status == expected
    assert bool(projection(library, proposed)["tablets"]) == eligible
    installation.refresh_from_db()
    if expected in ("LOST", "RETIRED"):
        assert installation.status == "REVOKED"


def capacity_mutation(scope, tablet, change):
    vehicle = TabletVehicleAssignment.objects.get(tablet=tablet, ended_at=None).vehicle
    if change == "retire_vehicle":
        return lambda: organization_services.retire_vehicle(actor=scope[0], vehicle=vehicle)
    if change == "update_vehicle":
        return lambda: organization_services.update_vehicle(
            actor=scope[0],
            vehicle=vehicle,
            display_name=vehicle.display_name,
            call_sign=vehicle.call_sign,
            asset_identifier=vehicle.asset_identifier,
            active=False,
        )
    if change == "reassign":
        target = Vehicle.objects.create(
            department=scope[3], station=scope[6], display_name="Target"
        )
        return lambda: assign_tablet_vehicle(tablet=tablet, vehicle=target, actor=scope[0])
    if change == "legacy_deactivate":
        return lambda: organization_services.deactivate_tablet(actor=scope[0], tablet=tablet)
    return lambda: getattr(tablet_services, change)(actor=scope[0], tablet=tablet)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("phase", ["confirm", "consume"])
@pytest.mark.parametrize(
    "change",
    [
        "retire_vehicle",
        "update_vehicle",
        "reassign",
        "deactivate_tablet",
        "activate_tablet",
        "mark_tablet_lost",
        "retire_tablet",
        "legacy_deactivate",
    ],
)
def test_review_rechecks_population_after_concurrent_mutation(scope, pdf_pipeline, phase, change):
    library = library_for(scope)
    tablet, _ = tablet_for(scope, free=10, total=1000)
    if change == "activate_tablet":
        # Keep one warning tablet and add a second eligible asset after review.
        tablet, _ = tablet_for(scope)
        Tablet.objects.filter(pk=tablet.pk).update(status="INACTIVE")
    data = pdf().read()
    review = warning(scope[0], library, data)
    if phase == "consume":
        approve(scope[0], review)
    mutation = capacity_mutation(scope, tablet, change)

    def use_review():
        with pytest.raises(services.LibraryError, match="changed|stale"):
            if phase == "confirm":
                approve(scope[0], review)
            else:
                services.upload_pdf(
                    actor=scope[0],
                    library_id=library.id,
                    upload=upload_bytes(data),
                    approval_id=review.id,
                )

    outside = services.get_library(scope[2], department_id=scope[4].id)

    def independent_department():
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
            assert projection(outside, operation())["tablets"] == []

    serialized_operations(mutation, use_review, while_blocked=independent_department)
    review.refresh_from_db()
    assert review.consumed_at is None
    assert review.decision == ("pending" if phase == "confirm" else "approved")
    assert not Entry.objects.exists()
    assert not StorageImpact.objects.exists()
    assert not AuditEvent.objects.filter(action="file_library.capacity_override_consumed").exists()
    current = projection(library, review.operation)
    assert capacity.fingerprint(current) != review.fingerprint
    if change in ("retire_vehicle", "update_vehicle", "legacy_deactivate"):
        assert not TabletVehicleAssignment.objects.filter(tablet=tablet, ended_at=None).exists()
    if change in ("retire_vehicle", "update_vehicle"):
        assert not Vehicle.objects.get(tablet_assignments__tablet=tablet).active
        assert AuditEvent.objects.filter(action="organization.vehicle_retired").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("retirement_first", [True, False])
def test_vehicle_retirement_and_reassignment_serialize_under_capacity_lock(scope, retirement_first):
    tablet, _ = tablet_for(scope)
    original = TabletVehicleAssignment.objects.get(tablet=tablet)
    target = Vehicle.objects.create(department=scope[3], station=scope[6], display_name="Target")

    def retirement():
        return organization_services.retire_vehicle(actor=scope[0], vehicle=original.vehicle)

    def reassignment():
        return assign_tablet_vehicle(actor=scope[0], tablet=tablet, vehicle=target)

    serialized_operations(
        retirement if retirement_first else reassignment,
        reassignment if retirement_first else retirement,
    )
    original.refresh_from_db()
    assert original.ended_at is not None
    assert not Vehicle.objects.get(pk=original.vehicle_id).active
    assert TabletVehicleAssignment.objects.get(tablet=tablet, ended_at=None).vehicle_id == target.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("phase", ["confirm", "consume"])
def test_review_commits_before_waiting_vehicle_retirement(scope, pdf_pipeline, phase):
    library = library_for(scope)
    tablet, _ = tablet_for(scope, free=10, total=1000)
    assignment = TabletVehicleAssignment.objects.get(tablet=tablet)
    data = pdf().read()
    review = warning(scope[0], library, data)
    if phase == "consume":
        approve(scope[0], review)

    def use_review():
        if phase == "confirm":
            return approve(scope[0], review)
        return services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=upload_bytes(data), approval_id=review.id
        )

    serialized_operations(
        use_review,
        lambda: organization_services.retire_vehicle(actor=scope[0], vehicle=assignment.vehicle),
    )
    review.refresh_from_db()
    assignment.refresh_from_db()
    library.refresh_from_db()
    assert assignment.ended_at is not None
    assert not Vehicle.objects.get(pk=assignment.vehicle_id).active
    assert review.decision == "approved"
    assert (review.consumed_at is not None) == (phase == "consume")
    assert Entry.objects.count() == StorageImpact.objects.count() == (phase == "consume")
    assert library.source_revision == (phase == "consume")
    if phase == "confirm":
        with pytest.raises(services.LibraryError, match="stale"):
            services.upload_pdf(
                actor=scope[0],
                library_id=library.id,
                upload=upload_bytes(data),
                approval_id=review.id,
            )
        review.refresh_from_db()
        assert review.consumed_at is None


@pytest.mark.django_db(transaction=True)
def test_station_reactivation_shares_capacity_boundary(scope):
    library = library_for(scope)
    tablet_for(scope)
    station = scope[5]
    type(station).objects.filter(pk=station.pk).update(active=False)
    proposed = operation()
    before, _ = serialized_operations(
        lambda: projection(library, proposed),
        lambda: organization_services.update_station(
            actor=scope[0],
            station=station,
            name=station.name,
            short_code=station.short_code,
            active=True,
        ),
    )
    assert before["tablets"] == []
    station.refresh_from_db()
    assert station.active
    assert len(projection(library, proposed)["tablets"]) == 1
