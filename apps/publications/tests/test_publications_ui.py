"""Phase 4B scope-centric Publications UI regressions."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications import services
from apps.publications.builders import build_source_payload, source_fingerprint
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path
from apps.publications.registry import get_dataset_definition
from apps.publications.services import (
    build_staged_publication,
    claim_next_job,
    delete_staged_publication,
    mark_dirty,
    stage_publication_update,
)
from apps.reference_data.services import create_hydrant, delete_hydrant, update_hydrant


@pytest.fixture
def publication_ui_context(db):
    admin = User.objects.create_user("publication-ui@example.test", "UI Admin", "safe-password")
    outsider = User.objects.create_user(
        "publication-outsider@example.test", "Outsider", "safe-password"
    )
    department = Department.objects.create(
        name="Publication UI", short_code="PUI", created_by=admin
    )
    other_department = Department.objects.create(
        name="Other UI", short_code="OUI", created_by=outsider
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="UI Station", short_code="UIS")
    return admin, outsider, department, other_department, station


def _publication(*, department, scope, version_number, status):
    publication_id = uuid.uuid4()
    ready = status in (DatasetPublication.Status.PUBLISHED, DatasetPublication.Status.SUPERSEDED)
    fields = {
        "id": publication_id,
        "department": department,
        "station": scope.station,
        "dataset_type_code": scope.dataset_type_code,
        "scope_state": scope,
        "version_number": version_number,
        "schema_version": 1,
        "source_revision": 1,
        "status": status,
    }
    if ready:
        fields.update(
            artifact_ready=True,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
            artifact_path=publication_artifact_relative_path(
                department_id=department.id, publication_id=publication_id
            ),
            artifact_size=1,
            artifact_sha256="a" * 64,
            artifact_nonce=b"n" * 12,
            artifact_wrapped_cek=b"w" * 40,
            artifact_encryption_algorithm="AES-256-GCM",
            artifact_wrapping_algorithm="AES-KW-RFC3394",
            artifact_kek_version="test",
            artifact_signature=b"s" * 64,
            artifact_signature_algorithm="Ed25519",
            artifact_signing_key_version="test",
        )
    return DatasetPublication.objects.create(**fields)


def _current_scope(*, department, dataset_type_code="department_hydrants"):
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code=dataset_type_code
    )
    current = _publication(
        department=department,
        scope=scope,
        version_number=7,
        status=DatasetPublication.Status.PUBLISHED,
    )
    scope.latest_built_publication = current
    scope.current_published_publication = current
    scope.save(update_fields=("latest_built_publication", "current_published_publication"))
    _record_current_fingerprint(scope, current)
    return scope, current


def _record_current_fingerprint(scope, current):
    current.source_fingerprint = source_fingerprint(
        definition=get_dataset_definition(scope.dataset_type_code),
        department=scope.department,
        station=scope.station,
    )
    current.source_snapshot = build_source_payload(
        definition=get_dataset_definition(scope.dataset_type_code),
        department=scope.department,
        station=scope.station,
    )
    current.save(update_fields=("source_fingerprint", "source_snapshot"))
    scope.current_source_fingerprint = current.source_fingerprint
    scope.save(update_fields=("current_source_fingerprint",))


@pytest.mark.django_db(transaction=True)
def test_primary_list_is_one_row_per_scope_with_current_version_and_detail_link(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    historical = _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    client.force_login(admin)

    response = client.get(reverse("publications-list", args=(department.id,)))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Dataset / Scope" in content
    assert "Current publication" in content
    assert "Update" in content
    assert "Last changed" in content
    assert content.count("department_hydrants") == 0
    assert content.count("Department hydrants") == 1
    assert f"v{current.version_number}" in content
    assert f"v{historical.version_number}" not in content
    assert reverse("publications-scope-detail", args=(scope.id,)) in content
    assert "View details" not in content
    assert "Previous versions" not in content


@pytest.mark.django_db(transaction=True)
def test_update_is_separate_from_current_and_building_row_polls_only_itself(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    # Model an unpublished source change behind the active v7 so a later
    # failed v8 remains visible as the scope update state.
    current.source_fingerprint = "a" * 64
    current.save(update_fields=("source_fingerprint",))
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.STAGED,
    )
    job = PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=1,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
        status=PublicationJob.Status.RUNNING,
        build_publication=staged,
    )
    staged.status = DatasetPublication.Status.BUILDING
    staged.save(update_fields=("status",))
    client.force_login(admin)

    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert f"v{current.version_number}" in content
    assert "Current" in content
    assert "Building v8" in content
    assert 'hx-trigger="every 1s"' in content
    assert reverse("publications-scope-row", args=(scope.id,)) in content
    assert "Cancel build" in content
    assert "Delete staged" not in content

    job.status = PublicationJob.Status.FAILED
    job.save(update_fields=("status",))
    staged.status = DatasetPublication.Status.FAILED
    staged.save(update_fields=("status",))
    failed = client.get(reverse("publications-scope-row", args=(scope.id,))).content.decode()
    assert "v8" in failed
    assert "Failed" in failed
    assert "every 1s" not in failed


@pytest.mark.django_db(transaction=True)
def test_list_and_building_row_use_stored_fingerprints_without_rebuilding_source(
    client, monkeypatch, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, _current = _current_scope(department=department)
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.BUILDING,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=scope.source_revision,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
        status=PublicationJob.Status.RUNNING,
        build_publication=staged,
    )

    def source_rebuild_called(*args, **kwargs):
        raise AssertionError(
            "Publication status rendering must not rebuild canonical source payloads."
        )

    monkeypatch.setattr("apps.publications.builders.build_source_payload", source_rebuild_called)
    monkeypatch.setattr("apps.publications.builders.source_fingerprint", source_rebuild_called)
    client.force_login(admin)

    assert client.get(reverse("publications-list", args=(department.id,))).status_code == 200
    response = client.get(reverse("publications-scope-row", args=(scope.id,)))
    assert response.status_code == 200
    assert 'hx-trigger="every 1s"' in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_staged_action_modal_is_scope_authorized_and_explains_history(
    client, publication_ui_context
):
    admin, outsider, department, _, _ = publication_ui_context
    scope, _ = _current_scope(department=department)
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.STAGED,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=1,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
        status=PublicationJob.Status.PENDING,
        build_publication=staged,
    )
    modal_url = reverse("publications-lifecycle-modal", args=(staged.id, "delete-staged"))
    client.force_login(admin)

    list_content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Delete staged" in list_content
    assert "Build now" in list_content
    assert "Roll back" not in list_content
    modal = client.get(modal_url, HTTP_HX_REQUEST="true")
    assert modal.status_code == 200
    assert "Delete staged publication v8?" in modal.content.decode()
    assert "not reused" in modal.content.decode()

    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()
    build_now = client.post(
        reverse("publications-scope-build-now", args=(scope.id,)), HTTP_HX_REQUEST="true"
    )
    assert build_now.status_code == 200
    assert 'hx-trigger="every 1s"' in build_now.content.decode()
    assert DatasetPublication.objects.get(pk=staged.id).version_number == 8

    deleted = client.post(modal_url, HTTP_HX_REQUEST="true")
    assert deleted.status_code == 204
    assert deleted["HX-Redirect"] == reverse("publications-scope-detail", args=(scope.id,))
    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.CANCELLED

    client.force_login(outsider)
    assert client.get(modal_url, HTTP_HX_REQUEST="true").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_htmx_publication_mutations_redirect_to_reauthentication(client, publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    scope, _ = _current_scope(department=department)
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.STAGED,
    )
    client.force_login(admin)

    action_urls = (
        reverse("publications-scope-stage-update", args=(scope.id,)),
        reverse("publications-scope-build-now", args=(scope.id,)),
        reverse("publications-lifecycle-modal", args=(staged.id, "delete-staged")),
        reverse("publications-lifecycle-modal", args=(staged.id, "rollback")),
    )
    for action_url in action_urls:
        response = client.post(action_url, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert response.content == b""
        assert response["HX-Redirect"].startswith(reverse("accounts-reauthenticate"))

    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.STAGED


@pytest.mark.django_db(transaction=True)
def test_cancel_modal_reports_a_build_that_finished_first(client, publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    _scope, current = _current_scope(department=department)
    client.force_login(admin)
    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()

    response = client.post(
        reverse("publications-lifecycle-modal", args=(current.id, "cancel")),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert (
        "finished before cancellation could take effect and is now current"
        in response.content.decode()
    )


@pytest.mark.django_db(transaction=True)
def test_scope_detail_has_bounded_history_and_detail_only_successful_delete_actions(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    predecessor = _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    failed = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.FAILED,
    )
    client.force_login(admin)

    list_content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Delete publication" not in list_content
    detail = client.get(reverse("publications-scope-detail", args=(scope.id,)))
    content = detail.content.decode()
    assert detail.status_code == 200
    assert "Department hydrants" in content
    assert "Version history" in content
    history = content[content.index("Version history") :]
    assert history.index("v8") < history.index("v7") < history.index("v6")
    assert "Roll back to this version" in content
    assert reverse("publications-lifecycle-modal", args=(predecessor.id, "rollback")) in content
    assert reverse("publications-lifecycle-modal", args=(current.id, "delete")) in content
    assert reverse("publications-lifecycle-modal", args=(failed.id, "rollback")) not in content


@pytest.mark.django_db(transaction=True)
def test_current_row_does_not_offer_rollback_when_only_newer_superseded_version_exists(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, _ = _current_scope(department=department)
    _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    client.force_login(admin)

    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()

    assert "Roll back" not in content


@pytest.mark.django_db(transaction=True)
def test_filters_are_server_side_and_count_scopes_not_attempts(client, publication_ui_context):
    admin, _, department, _, station = publication_ui_context
    scope, _ = _current_scope(department=department)
    _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    mark_dirty(
        actor=admin,
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )
    client.force_login(admin)

    response = client.get(
        reverse("publications-list", args=(department.id,)),
        {"state": "scheduled"},
        HTTP_HX_REQUEST="true",
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert "Showing 1 of 1 publication scope" in content
    assert "Station personnel · UIS" in content
    assert "Department hydrants" not in content


@pytest.mark.django_db(transaction=True)
def test_cancelled_attempt_leaves_canonical_change_dirty_and_manual_stage_uses_new_version(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    _record_current_fingerprint(scope, current)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-003",
    )
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    assert staged.version_number == 8

    delete_staged_publication(actor=admin, publication=staged)
    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.CANCELLED
    assert hydrant.external_identifier == "FB-003"

    client.force_login(admin)
    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Changes not published" in content
    assert "Stage update" in content

    stage_publication_update(
        actor=admin,
        department=department,
        dataset_type_code=scope.dataset_type_code,
    )
    restaged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    assert restaged.version_number == 9
    job = build_staged_publication(actor=admin, scope=scope)
    assert job.build_publication_id == restaged.id
    assert job.not_before is not None


@pytest.mark.django_db(transaction=True)
def test_reverting_to_active_source_becomes_clean_without_reusing_cancelled_attempt(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    _record_current_fingerprint(scope, current)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-003",
    )
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    delete_staged_publication(actor=admin, publication=staged)
    delete_hydrant(actor=admin, hydrant=hydrant)

    client.force_login(admin)
    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Changes not published" not in content
    assert "Stage update" not in content
    assert DatasetPublication.objects.filter(scope_state=scope).count() == 2


@pytest.mark.django_db(transaction=True)
def test_revert_cancels_an_unstarted_redundant_attempt(publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    _record_current_fingerprint(scope, current)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-003",
    )
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )

    delete_hydrant(actor=admin, hydrant=hydrant)

    staged.refresh_from_db()
    staged_job = PublicationJob.objects.get(build_publication=staged)
    assert staged.status == DatasetPublication.Status.CANCELLED
    assert staged_job.status == PublicationJob.Status.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_staged_candidate_coalesces_latest_source_and_freezes_when_claimed(publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    _record_current_fingerprint(scope, current)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-002",
        street="First street",
    )
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    first_fingerprint = staged.source_fingerprint

    update_hydrant(actor=admin, hydrant=hydrant, street="Second street")
    staged.refresh_from_db()
    assert DatasetPublication.objects.filter(scope_state=scope).count() == 2
    assert staged.status == DatasetPublication.Status.STAGED
    assert staged.source_fingerprint != first_fingerprint

    build_staged_publication(actor=admin, scope=scope)
    claimed = claim_next_job()
    assert claimed is not None
    staged.refresh_from_db()
    frozen = staged.source_fingerprint
    assert frozen == source_fingerprint(
        definition=get_dataset_definition(scope.dataset_type_code),
        department=department,
        station=None,
    )
    update_hydrant(actor=admin, hydrant=hydrant, street="Third street")
    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.BUILDING
    assert staged.source_fingerprint == frozen


@pytest.mark.django_db(transaction=True)
def test_inspect_changes_uses_the_latest_coalesced_staged_snapshot(client, publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-002",
        street="First street",
    )
    initial_staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    delete_staged_publication(actor=admin, publication=initial_staged)
    _record_current_fingerprint(scope, current)

    update_hydrant(actor=admin, hydrant=hydrant, street="Second street")
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    update_hydrant(actor=admin, hydrant=hydrant, street="Third street")
    staged.refresh_from_db()
    assert staged.source_snapshot["features"][0]["properties"]["street"] == "Third street"

    client.force_login(admin)
    response = client.get(reverse("publications-inspect-changes", args=(scope.id,)))

    assert response.status_code == 200
    content = response.content.decode()
    assert f"Changes in publication v{staged.version_number}" in content
    assert "Street: First street → Third street" in content


@pytest.mark.django_db(transaction=True)
def test_claim_freeze_wins_over_later_canonical_edit_with_real_scope_lock(
    monkeypatch, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-002",
        street="First street",
    )
    initial_staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    delete_staged_publication(actor=admin, publication=initial_staged)
    _record_current_fingerprint(scope, current)
    update_hydrant(actor=admin, hydrant=hydrant, street="Second street")
    staged = DatasetPublication.objects.get(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    )
    build_staged_publication(actor=admin, scope=scope)

    original_snapshot = services._current_source_snapshot
    snapshot_taken = threading.Event()
    release_claim = threading.Event()
    blocked_once = False

    def pause_after_claim_snapshot(*, scope):
        nonlocal blocked_once
        snapshot = original_snapshot(scope=scope)
        if threading.current_thread() is not threading.main_thread() and not blocked_once:
            blocked_once = True
            snapshot_taken.set()
            assert release_claim.wait(timeout=10)
        return snapshot

    monkeypatch.setattr(services, "_current_source_snapshot", pause_after_claim_snapshot)

    def claim_in_worker():
        close_old_connections()
        try:
            return claim_next_job()
        finally:
            close_old_connections()

    def mutate_after_freeze_started():
        close_old_connections()
        try:
            worker_admin = User.objects.get(pk=admin.pk)
            worker_hydrant = type(hydrant).objects.get(pk=hydrant.pk)
            update_hydrant(actor=worker_admin, hydrant=worker_hydrant, street="Third street")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim = executor.submit(claim_in_worker)
        assert snapshot_taken.wait(timeout=10)
        mutation = executor.submit(mutate_after_freeze_started)
        release_claim.set()
        claimed = claim.result(timeout=10)
        mutation.result(timeout=10)

    assert claimed is not None
    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.BUILDING
    frozen_fingerprint = staged.source_fingerprint
    assert frozen_fingerprint != source_fingerprint(
        definition=get_dataset_definition(scope.dataset_type_code),
        department=department,
        station=None,
    )
    assert not PublicationJob.objects.filter(
        scope_state=scope, status=PublicationJob.Status.PENDING
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_noop_hydrant_edit_does_not_mark_scope_or_stage_a_publication(publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    hydrant = create_hydrant(
        actor=admin,
        department=department,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-002",
        street="Main Street",
        location=None,
    )
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    revision = scope.source_revision
    attempts = DatasetPublication.objects.filter(scope_state=scope).count()

    update_hydrant(
        actor=admin,
        hydrant=hydrant,
        longitude=10.0,
        latitude=53.0,
        external_identifier="FB-002",
        street="Main Street",
        house_number="",
        location=None,
        hydrant_type="",
        diameter_mm=None,
        status="ACTIVE",
    )

    scope.refresh_from_db()
    assert scope.source_revision == revision
    assert DatasetPublication.objects.filter(scope_state=scope).count() == attempts
