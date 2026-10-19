from datetime import timedelta
from pathlib import Path
from time import time

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.personnel.models import PersonnelRetentionPolicy
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def stage4_scope(client, db):
    admin = User.objects.create_user("stage4-admin@example.test", "Department Admin", "password")
    station_admin = User.objects.create_user(
        "stage4-station@example.test", "Station Admin", "password"
    )
    other_admin = User.objects.create_user("stage4-other@example.test", "Other Admin", "password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other_department = Department.objects.create(
        name="Bravo", short_code="BRV", created_by=other_admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(
        user=other_admin, department=other_department, created_by=other_admin
    )
    station = Station.objects.create(department=department, name="Alpha One", short_code="A1")
    other_station = Station.objects.create(
        department=other_department, name="Bravo One", short_code="B1"
    )
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="HLF 1")
    tablet = Tablet.objects.create(
        department=department,
        display_name="Stale tablet",
        status=Tablet.Status.ACTIVE,
        created_by=admin,
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=admin
    )
    AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid="11111111-1111-1111-1111-111111111111",
        credential_hash="a" * 64,
        status=AppInstallation.Status.STALE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=b"key",
        hpke_ciphersuite="test",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() - timedelta(days=1),
    )
    Tablet.objects.create(
        department=department,
        display_name="Lost tablet",
        status=Tablet.Status.LOST,
        created_by=admin,
    )
    client.force_login(admin)
    return {
        "admin": admin,
        "station_admin": station_admin,
        "other_admin": other_admin,
        "department": department,
        "other_department": other_department,
        "station": station,
        "other_station": other_station,
    }


def _recent_reauth(client):
    session = client.session
    session["recent_reauthentication_at"] = time()
    session.save()


@pytest.mark.django_db
def test_department_navigation_overview_and_topbar_are_scoped(client, stage4_scope):
    department = stage4_scope["department"]
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    content = response.content.decode()
    for label in (
        "Data Hub",
        "Publications",
        "Stations",
        "Tablets",
        "Administrator Accounts",
        "System Settings",
        "Audit Logs",
        "Requires attention",
        "stale tablet installation",
        "marked lost",
    ):
        assert label in content
    for removed_label in ("Hydrants", "Fire Plans", "KLGV Plans", "Vehicles", "Imports"):
        assert removed_label not in content
    assert reverse("tablet-list", args=(department.id,)) in content
    assert "Attention" in content and "aria-label=" in content


@pytest.mark.django_db
def test_station_overview_is_fixed_to_its_authorized_station(client, stage4_scope):
    client.force_login(stage4_scope["station_admin"])
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "Station administration" in content
    assert "Administrator Accounts" not in content
    assert "System Settings" not in content
    assert "Audit Logs" not in content
    assert stage4_scope["other_station"].name not in content


@pytest.mark.django_db
def test_attention_indicator_is_quiet_without_authoritative_attention(client, db):
    admin = User.objects.create_user("quiet-admin@example.test", "Quiet", "password")
    department = Department.objects.create(name="Quiet", short_code="QUI", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    client.force_login(admin)
    response = client.get(reverse("dashboard"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "No current attention items require action." in content
    assert "btn-warning" not in content
    assert "No items require attention" in content


@pytest.mark.django_db
def test_department_settings_are_validated_scoped_and_audited(client, stage4_scope):
    department = stage4_scope["department"]
    url = reverse("portal-department-settings", args=(department.id,))
    response = client.get(url)
    assert response.status_code == 200
    assert "Stale-installation sweeps remain" in response.content.decode()
    _recent_reauth(client)
    invalid = client.post(url, {"tablet_lease_days": "2", "retention_days": "30"})
    assert invalid.status_code == 200
    department.refresh_from_db()
    assert department.tablet_lease_days == 7
    valid = client.post(url, {"tablet_lease_days": "14", "retention_days": "45"})
    assert valid.status_code == 302
    department.refresh_from_db()
    assert department.tablet_lease_days == 14
    assert PersonnelRetentionPolicy.objects.get(
        department=department
    ).retention_period == timedelta(days=45)
    assert AuditEvent.objects.filter(
        action="authorization.department_tablet_lease_changed", department=department
    ).exists()
    assert AuditEvent.objects.filter(
        action="personnel.retention_policy_changed", department=department
    ).exists()
    assert (
        client.get(
            reverse("portal-department-settings", args=(stage4_scope["other_department"].id,))
        ).status_code
        == 403
    )
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(stage4_scope["admin"])
    assert (
        csrf_client.post(url, {"tablet_lease_days": "21", "retention_days": "60"}).status_code
        == 403
    )


@pytest.mark.django_db
def test_department_audit_is_bounded_safe_and_department_scoped(client, stage4_scope):
    department = stage4_scope["department"]
    other_department = stage4_scope["other_department"]
    actor = stage4_scope["admin"]
    for index in range(101):
        record_event(
            action=f"test.department_event_{index}",
            actor_user=actor,
            department=department,
            target_type="test",
            metadata={"safe": "value"},
        )
    record_event(
        action="test.other_department_event",
        actor_user=stage4_scope["other_admin"],
        department=other_department,
        target_type="test",
        metadata={"secret": "must not render"},
    )
    response = client.get(reverse("portal-department-audit", args=(department.id,)))
    assert response.status_code == 200
    assert len(response.context["events"]) == 100
    content = response.content.decode()
    assert "table-responsive" not in content and "Page 1 of 2" in content
    assert "test.other_department_event" not in content
    assert "must not render" not in content
    assert "No additional displayable context." in content
    filtered = client.get(
        reverse("portal-department-audit", args=(department.id,)), {"q": "event_100"}
    )
    assert filtered.status_code == 200
    assert [event.action for event in filtered.context["events"]] == ["test.department_event_100"]
    assert (
        client.get(reverse("portal-department-audit", args=(other_department.id,))).status_code
        == 403
    )


@pytest.mark.django_db
def test_department_accounts_are_bounded_and_mutations_require_reauth(client, stage4_scope):
    department = stage4_scope["department"]
    url = reverse("portal-department-manage", args=(department.id,))
    for index in range(101):
        user = User.objects.create_user(f"stage4-{index:03}@example.test", "Account", "password")
        DepartmentMembership.objects.create(
            user=user, department=department, created_by=stage4_scope["admin"]
        )
    response = client.get(url)
    assert response.status_code == 200
    assert len(response.context["administrators"]) == 100
    assert "Page 1 of 2" in response.content.decode()
    provision_url = reverse("portal-administrator-provision", args=(department.id,))
    invalid_provision = client.post(provision_url, {"email": "not-an-email", "display_name": ""})
    assert invalid_provision.status_code == 200
    assert b"Enter a valid email address" in invalid_provision.content
    candidate = User.objects.get(email="stage4-000@example.test")
    membership = DepartmentMembership.objects.get(user=candidate, department=department)
    revoke_url = reverse("portal-department-admin-revoke", args=(department.id, membership.id))
    modal = client.get(revoke_url, HTTP_HX_REQUEST="true")
    assert modal.status_code == 200
    assert b'<div class="modal fade"' in modal.content
    assert b"Revoke administrator access" in modal.content
    blocked = client.post(revoke_url)
    assert blocked.status_code == 302
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.ACTIVE
    _recent_reauth(client)
    revoked = client.post(revoke_url, HTTP_HX_REQUEST="true")
    assert revoked.status_code == 204
    assert revoked["HX-Redirect"] == url
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.REVOKED
    assert AuditEvent.objects.filter(
        action="authorization.department_admin_revoked", department=department
    ).exists()
    assert (
        client.get(
            reverse("portal-department-manage", args=(stage4_scope["other_department"].id,))
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_htmx_administrator_revoke_uses_the_shared_reauthentication_redirect(client, stage4_scope):
    department = stage4_scope["department"]
    candidate = User.objects.create_user("reauth-candidate@example.test", "Candidate", "password")
    membership = DepartmentMembership.objects.create(
        user=candidate,
        department=department,
        created_by=stage4_scope["admin"],
    )
    revoke_url = reverse("portal-department-admin-revoke", args=(department.id, membership.id))

    response = client.post(revoke_url, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert response.content == b""
    assert response["HX-Redirect"].startswith(reverse("accounts-reauthenticate"))
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.ACTIVE


@pytest.mark.django_db
def test_administrator_provision_modal_fragments_and_role_assignment(client, stage4_scope):
    department = stage4_scope["department"]
    station = stage4_scope["station"]
    department_url = reverse("portal-administrator-provision", args=(department.id,))
    station_url = reverse(
        "portal-station-administrator-provision", args=(department.id, station.id)
    )

    for url, label in ((department_url, b"Department Admin"), (station_url, b"Station Admin")):
        response = client.get(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert b'<div class="modal fade show"' in response.content
        assert label in response.content

    invalid = client.post(
        station_url,
        {"email": "invalid", "display_name": ""},
        HTTP_HX_REQUEST="true",
    )
    assert invalid.status_code == 200
    assert b"Enter a valid email address" in invalid.content
    assert b'<div class="modal fade show"' in invalid.content

    invalid_full_page = client.post(department_url, {"email": "invalid", "display_name": ""})
    assert invalid_full_page.status_code == 200
    assert b"<html" in invalid_full_page.content.lower()
    assert b"Enter a valid email address" in invalid_full_page.content

    response = client.post(
        department_url,
        {"email": "new-department@example.test", "display_name": "New Department"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response["HX-Redirect"].startswith(reverse("accounts-reauthenticate"))
    assert client.session["pending_reauth"]["return_url"] == reverse(
        "portal-department-manage", args=(department.id,)
    )
    assert not User.objects.filter(email="new-department@example.test").exists()

    _recent_reauth(client)
    success = client.post(
        department_url,
        {"email": "new-department@example.test", "display_name": "New Department"},
        HTTP_HX_REQUEST="true",
    )
    assert success.status_code == 200
    assert [template.name for template in success.templates] == [
        "portal/_administrator_setup_link_modal.html"
    ]
    assert b"single-use setup link" in success.content
    assert b"<html" not in success.content.lower()
    department_user = User.objects.get(email="new-department@example.test")
    assert DepartmentMembership.objects.filter(
        user=department_user, department=department, status="ACTIVE"
    ).exists()

    full_page = client.post(
        station_url,
        {"email": "new-station@example.test", "display_name": "New Station"},
    )
    assert full_page.status_code == 200
    assert "portal/setup_link.html" in [template.name for template in full_page.templates]
    assert b"<html" in full_page.content.lower()
    station_user = User.objects.get(email="new-station@example.test")
    assert StationAdminAssignment.objects.filter(
        user=station_user, station=station, status="ACTIVE"
    ).exists()


@pytest.mark.django_db
def test_department_search_lists_use_personnel_live_contract_and_results_fragments(
    client, stage4_scope
):
    department = stage4_scope["department"]
    for template, result_id in (
        ("templates/portal/department_manage.html", "department-administrator-results"),
        ("templates/portal/department_audit.html", "department-audit-results"),
    ):
        content = (Path.cwd() / template).read_text(encoding="utf-8")
        assert "input changed delay:1s" in content
        assert f"#{result_id}" in content
        assert "hx-include" in content and "hx-push-url" in content

    response = client.get(
        reverse("portal-department-manage", args=(department.id,)),
        {"q": stage4_scope["admin"].email},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert 'id="department-administrator-results"' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("station_scope", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_provision_duplicate_email_is_a_field_error_without_authority_changes(
    client, stage4_scope, station_scope, active
):
    from apps.accounts.models import AccountSetupToken

    existing = stage4_scope["other_admin"]
    existing.is_active = active
    existing.save(update_fields=("is_active",))
    department = stage4_scope["department"]
    url = (
        reverse(
            "portal-station-administrator-provision",
            args=(department.id, stage4_scope["station"].id),
        )
        if station_scope
        else reverse("portal-administrator-provision", args=(department.id,))
    )
    _recent_reauth(client)
    counts = (User.objects.count(), AccountSetupToken.objects.count(), AuditEvent.objects.count())
    response = client.post(
        url,
        {"email": f"  {existing.email.upper()}  ", "display_name": "Duplicate"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert list(response.context["form"].errors) == ["email"]
    assert b"An account with this email address already exists." in response.content
    assert b"invalid-feedback d-block" in response.content
    assert b'id="department-admin-modal"' in response.content
    assert b"single-use setup link" not in response.content
    assert counts == (
        User.objects.count(),
        AccountSetupToken.objects.count(),
        AuditEvent.objects.count(),
    )
    assert not DepartmentMembership.objects.filter(user=existing, department=department).exists()
    assert not StationAdminAssignment.objects.filter(
        user=existing, station__department=department
    ).exists()
    existing.refresh_from_db()
    assert existing.is_active == active
    assert existing.display_name == "Other Admin"


@pytest.mark.django_db
@pytest.mark.parametrize("station_scope", [False, True])
def test_provision_rejects_cross_department_scope(client, stage4_scope, station_scope):
    department = stage4_scope["other_department"]
    url = (
        reverse(
            "portal-station-administrator-provision",
            args=(department.id, stage4_scope["other_station"].id),
        )
        if station_scope
        else reverse("portal-administrator-provision", args=(department.id,))
    )
    _recent_reauth(client)
    response = client.post(
        url,
        {"email": "unauthorized-new@example.test", "display_name": "Unauthorized"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 403
    assert not User.objects.filter(email="unauthorized-new@example.test").exists()
