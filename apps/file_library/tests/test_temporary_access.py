# ruff: noqa: F811
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.authorization.services import set_department_tablet_lease, suspend_department_admin
from apps.file_library import services
from apps.file_library.models import Content, TemporaryStationAccess
from apps.file_library.publication import publish_now
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.organizations.services import update_station
from apps.publications.models import DatasetScopeState

pytestmark = pytest.mark.django_db


def _activate(actor, station):
    return services.activate_temporary_access(actor=actor, station=station)


def test_one_station_switch_release_expiry_and_authority_loss(scope):
    admin, _station_admin, _other, _department, _outside, station, second = scope
    first = _activate(admin, station)
    assert first.expires_at == first.activated_at + timedelta(hours=6)
    same = _activate(admin, station)
    assert same.id == first.id

    switched = _activate(admin, second)
    first.refresh_from_db()
    assert first.revocation_reason == "switched"
    assert switched.station == second
    assert TemporaryStationAccess.objects.filter(user=admin, revoked_at__isnull=True).count() == 1
    assert AuditEvent.objects.filter(
        action="file_library.temporary_access_switched", station=second
    ).exists()

    services.revoke_temporary_access(actor=admin)
    switched.refresh_from_db()
    assert switched.revocation_reason == "released"

    expired = _activate(admin, station)
    TemporaryStationAccess.objects.filter(pk=expired.pk).update(
        activated_at=timezone.now() - timedelta(hours=2),
        expires_at=timezone.now() - timedelta(hours=1),
    )
    assert services.active_temporary_access(admin) is None
    expired.refresh_from_db()
    assert expired.revocation_reason == "expired"

    invalid = _activate(admin, station)
    DepartmentMembership.objects.filter(user=admin).update(status="SUSPENDED")
    assert services.active_temporary_access(admin) is None
    invalid.refresh_from_db()
    assert invalid.revocation_reason == "authority_revoked"


def test_activation_rejects_cross_department_and_requires_confirmation_and_reauth(client, scope):
    admin, _station_admin, _other, _department, outside, station, _second = scope
    outside_station = outside.stations.create(name="Outside", short_code="OUT")
    with pytest.raises(PermissionDenied):
        _activate(admin, outside_station)

    client.force_login(admin)
    url = reverse("file-library-station-access-activate", args=(station.id,))
    assert client.post(url).status_code == 400
    response = client.post(url, {"confirm": "yes"})
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts-reauthenticate"))
    assert not TemporaryStationAccess.objects.filter(revoked_at__isnull=True).exists()
    assert client.session["pending_reauth"]["return_url"].startswith("/files/")

    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    activated = client.post(url, {"confirm": "yes"})
    access = TemporaryStationAccess.objects.get(revoked_at__isnull=True)
    station_library = access.station.library
    assert activated.status_code == 302
    assert activated.url == reverse("file-library", args=(station_library.id,))

    TemporaryStationAccess.objects.filter(pk=access.pk).update(
        activated_at=timezone.now() - timedelta(hours=2),
        expires_at=timezone.now() - timedelta(hours=1),
    )
    expired = client.get(reverse("file-library", args=(station_library.id,)))
    assert expired.status_code == 302
    assert expired.url != reverse("file-library", args=(station_library.id,))

    assert (
        client.post(
            reverse("file-library-station-access-activate", args=(outside_station.id,)),
            {"confirm": "yes"},
        ).status_code
        == 403
    )


def test_elevated_permission_boundary_and_read_only_ui(client, scope, pdf_pipeline):
    admin, station_admin, _other, _department, _outside, station, _second = scope
    station_library = services.get_library(station_admin, station_id=station.id)
    document = services.upload_pdf(actor=station_admin, library_id=station_library.id, upload=pdf())
    _activate(admin, station)

    assert (
        services.require_library_access(admin, station_library, permission="read")
        == services.ACCESS_TEMPORARY
    )
    with pytest.raises(PermissionDenied):
        services.require_library(admin, station_library)
    with pytest.raises(PermissionDenied):
        services.create_folder(actor=admin, library_id=station_library.id, name="Denied")
    with pytest.raises(PermissionDenied):
        services.edit_entry(
            actor=admin, library_id=station_library.id, entry_id=document.id, name="Denied.pdf"
        )
    with pytest.raises(PermissionDenied):
        services.upload_pdf(actor=admin, library_id=station_library.id, upload=pdf("denied.pdf"))
    with pytest.raises(PermissionDenied):
        publish_now(actor=admin, library_id=station_library.id)

    client.force_login(admin)
    page = client.get(reverse("file-library-explorer", args=(station_library.id,)))
    content = page.content.decode()
    assert page.status_code == 200
    assert "Temporary station access" in content
    assert "Preview" in content and "Download" in content and "Delete" in content
    for denied_label in ("Publish Now", "Upload PDFs", "Create folder", "Replace PDF", "Rename"):
        assert denied_label not in content
    assert (
        client.get(
            reverse("file-library-document", args=(station_library.id, document.id))
        ).status_code
        == 200
    )
    assert (
        client.post(
            reverse("file-library", args=(station_library.id,)),
            {"action": "folder", "name": "Denied"},
        ).status_code
        == 403
    )


def test_elevated_delete_uses_source_lifecycle_and_consumes_grant(scope, pdf_pipeline):
    admin, station_admin, _other, _department, _outside, station, _second = scope
    station_library = services.get_library(station_admin, station_id=station.id)
    document = services.upload_pdf(actor=station_admin, library_id=station_library.id, upload=pdf())
    content_id = document.content_id
    station_library.refresh_from_db()
    before = station_library.source_revision
    _activate(admin, station)

    access_mode = services.delete_entry(
        actor=admin, library_id=station_library.id, entry_id=document.id
    )
    station_library.refresh_from_db()
    assert access_mode == services.ACCESS_TEMPORARY
    assert station_library.source_revision == before + 1
    assert station_library.usage_bytes == 0
    station_scope = DatasetScopeState.objects.get(
        dataset_type_code="station_files", station=station
    )
    assert station_scope.source_revision == station_library.source_revision
    assert not DatasetScopeState.objects.filter(dataset_type_code="department_files").exists()
    assert services.active_temporary_access(admin) is None
    assert Content.objects.filter(pk=content_id).exists()
    assert services.collect_content(content_id)
    deleted = AuditEvent.objects.get(action="file_library.deleted", target_uuid=document.id)
    assert deleted.metadata["elevated"] is True
    assert AuditEvent.objects.filter(
        action="file_library.temporary_access_consumed_by_deletion",
        station=station,
        target_uuid=document.id,
    ).exists()


def test_department_library_mutation_clears_access(scope):
    admin, _station_admin, _other, _department, _outside, station, _second = scope
    library = library_for(scope)
    access = _activate(admin, station)
    services.rename_library(actor=admin, library_id=library.id, name="Renamed")
    access.refresh_from_db()
    assert access.revocation_reason == "department_mutation"


def test_authority_lifecycle_immediately_revokes_and_audits_access(scope):
    actor, _station_admin, _other, department, _outside, station, _second = scope
    target = User.objects.create_user("temporary@example.test", "Temporary", "password")
    membership = DepartmentMembership.objects.create(
        user=target, department=department, created_by=actor
    )
    access = _activate(target, station)

    suspend_department_admin(actor=actor, membership=membership)

    access.refresh_from_db()
    assert access.revocation_reason == "authority_revoked"
    event = AuditEvent.objects.get(
        action="file_library.temporary_access_authority_revoked", target_uuid=access.id
    )
    assert event.actor_user == actor
    assert event.metadata["access_user_id"] == str(target.id)


def test_expiry_audit_survives_rejected_mutation(scope):
    admin, station_admin, _other, _department, _outside, station, _second = scope
    library = services.get_library(station_admin, station_id=station.id)
    access = _activate(admin, station)
    TemporaryStationAccess.objects.filter(pk=access.pk).update(
        activated_at=timezone.now() - timedelta(hours=2),
        expires_at=timezone.now() - timedelta(hours=1),
    )

    with pytest.raises(PermissionDenied):
        services.create_folder(actor=admin, library_id=library.id, name="Denied")

    access.refresh_from_db()
    assert access.revocation_reason == "expired"
    assert AuditEvent.objects.filter(
        action="file_library.temporary_access_expired", target_uuid=access.id
    ).exists()


def test_successful_settings_mutation_revokes_but_noop_and_failure_do_not(scope):
    admin, _station_admin, _other, department, _outside, station, _second = scope
    first = _activate(admin, station)
    set_department_tablet_lease(
        actor=admin,
        department=department,
        tablet_lease_days=department.tablet_lease_days,
    )
    first.refresh_from_db()
    assert first.revoked_at is None

    with pytest.raises(ValueError):
        set_department_tablet_lease(actor=admin, department=department, tablet_lease_days=1)
    first.refresh_from_db()
    assert first.revoked_at is None

    set_department_tablet_lease(
        actor=admin,
        department=department,
        tablet_lease_days=department.tablet_lease_days + 1,
    )
    first.refresh_from_db()
    assert first.revocation_reason == "department_mutation"


def test_successful_station_configuration_revokes_but_noop_does_not(scope):
    admin, _station_admin, _other, _department, _outside, station, _second = scope
    access = _activate(admin, station)
    values = {
        "name": station.name,
        "short_code": station.short_code,
        "street": station.street,
        "house_number": station.house_number,
        "postal_code": station.postal_code,
        "city": station.city,
        "active": station.active,
    }
    update_station(actor=admin, station=station, **values)
    access.refresh_from_db()
    assert access.revoked_at is None

    update_station(actor=admin, station=station, **(values | {"name": "Changed"}))
    access.refresh_from_db()
    assert access.revocation_reason == "department_mutation"


def test_expiry_between_preflight_and_locked_delete_persists_after_denial(scope, monkeypatch):
    admin, station_admin, _other, _department, _outside, station, _second = scope
    library = services.get_library(station_admin, station_id=station.id)
    folder = services.create_folder(actor=station_admin, library_id=library.id, name="Keep")
    access = _activate(admin, station)
    original = services.active_temporary_access
    first = True

    def expire_after_preflight(actor, **kwargs):
        nonlocal first
        result = original(actor, **kwargs)
        if first:
            first = False
            TemporaryStationAccess.objects.filter(pk=access.pk).update(
                activated_at=timezone.now() - timedelta(hours=2),
                expires_at=timezone.now() - timedelta(hours=1),
            )
        return result

    monkeypatch.setattr(services, "active_temporary_access", expire_after_preflight)
    with pytest.raises(PermissionDenied):
        services.delete_entry(actor=admin, library_id=library.id, entry_id=folder.id)
    access.refresh_from_db()
    assert access.revocation_reason == "expired"
    assert library.entries.filter(pk=folder.id).exists()
    assert (
        AuditEvent.objects.filter(
            action="file_library.temporary_access_expired", target_uuid=access.id
        ).count()
        == 1
    )


def test_successful_administrator_provisioning_revokes_access(scope):
    from apps.authorization.services import provision_station_admin

    admin, _station_admin, _other, _department, _outside, station, _second = scope
    access = _activate(admin, station)
    provision_station_admin(
        actor=admin, station=station, email="new-station@example.test", display_name="New admin"
    )
    access.refresh_from_db()
    assert access.revocation_reason == "department_mutation"
