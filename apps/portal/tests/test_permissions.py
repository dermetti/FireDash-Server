import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.organizations.models import Department, Station, Vehicle


@pytest.fixture
def portal_data(db):
    system_admin = User.objects.create_user("system@example.test", "System", "safe-password")
    department_admin = User.objects.create_user(
        "department@example.test", "Department", "safe-password"
    )
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    outsider = User.objects.create_user("outsider@example.test", "Outsider", "safe-password")
    SystemRole.objects.create(user=system_admin)
    department = Department.objects.create(
        name="Department", short_code="DEP", created_by=system_admin
    )
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=system_admin
    )
    StationAdminAssignment.objects.create(
        user=station_admin, station=station, created_by=department_admin
    )
    return system_admin, department_admin, station_admin, outsider, department, station, vehicle


@pytest.mark.django_db
def test_dashboard_requires_authenticated_user(client):
    assert client.get(reverse("dashboard")).status_code == 302


@pytest.mark.django_db
def test_logout_revokes_protected_pages_and_prevents_html_caching(client, portal_data):
    _, department_admin, _, _, department, _, _ = portal_data
    client.force_login(department_admin)

    dashboard = client.get(reverse("dashboard"))

    assert dashboard.status_code == 200
    assert dashboard["Cache-Control"] == "no-store, private, must-revalidate"
    assert dashboard["Pragma"] == "no-cache"

    logout_response = client.post(reverse("accounts-logout"))

    assert logout_response.status_code == 302
    assert "_auth_user_id" not in client.session

    protected_pages = (
        reverse("dashboard"),
        reverse("personnel-list", args=(department.id,)),
        reverse("portal-stations", args=(department.id,)),
        reverse("publications-list", args=(department.id,)),
        reverse("tablet-list", args=(department.id,)),
        reverse("portal-department-audit", args=(department.id,)),
    )
    for url in protected_pages:
        assert client.get(url).status_code == 302

    htmx_fragment = client.get(
        reverse("portal-scoped-selector", args=(department.id, "stations")),
        HTTP_HX_REQUEST="true",
    )

    assert htmx_fragment.status_code == 302


@pytest.mark.django_db
def test_system_pages_are_system_admin_only(client, portal_data):
    system_admin, department_admin, _, _, department, _, _ = portal_data
    client.force_login(system_admin)
    assert client.get(reverse("dashboard")).status_code == 200
    assert client.get(reverse("portal-system-departments")).status_code == 200
    assert client.get(reverse("portal-system-department", args=(department.id,))).status_code == 200
    client.force_login(department_admin)
    assert client.get(reverse("portal-system-departments")).status_code == 403


@pytest.mark.django_db
def test_department_pages_are_department_admin_only(client, portal_data):
    _, department_admin, station_admin, _, department, _, _ = portal_data
    client.force_login(department_admin)
    assert client.get(reverse("portal-department-manage", args=(department.id,))).status_code == 200
    assert client.get(reverse("portal-stations", args=(department.id,))).status_code == 200
    client.force_login(station_admin)
    assert client.get(reverse("portal-department-manage", args=(department.id,))).status_code == 403
    assert client.get(reverse("portal-stations", args=(department.id,))).status_code == 403


@pytest.mark.django_db
def test_station_pages_are_limited_to_assigned_station(client, portal_data):
    _, _, station_admin, outsider, _, station, vehicle = portal_data
    client.force_login(station_admin)
    assert client.get(reverse("portal-station-manage", args=(station.id,))).status_code == 200
    assert client.get(reverse("portal-vehicles", args=(station.id,))).status_code == 200
    assert client.get(reverse("portal-vehicle-manage", args=(vehicle.id,))).status_code == 200
    client.force_login(outsider)
    assert client.get(reverse("portal-station-manage", args=(station.id,))).status_code == 403
    assert client.get(reverse("portal-vehicles", args=(station.id,))).status_code == 403


@pytest.mark.django_db
def test_department_admin_grants_and_revokes_explicit_station_scope(client, portal_data):
    _, department_admin, station_admin, _, department, station, _ = portal_data
    client.force_login(department_admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()

    assignment = StationAdminAssignment.objects.get(
        user=station_admin, station=station, active=True
    )
    response = client.post(
        reverse("portal-department-manage", args=(department.id,)),
        {"action": "revoke-station", "assignment_id": assignment.id},
    )
    assert response.status_code == 302
    assignment.refresh_from_db()
    assert not assignment.active
    assert AuditEvent.objects.filter(
        action="authorization.station_admin_revoked", target_uuid=assignment.id
    ).exists()

    client.force_login(station_admin)
    assert client.get(reverse("portal-station-manage", args=(station.id,))).status_code == 403

    client.force_login(department_admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    response = client.post(
        reverse("portal-department-manage", args=(department.id,)),
        {"action": "grant-station", "user_id": station_admin.id, "station_id": station.id},
    )
    assert response.status_code == 302
    assert AuditEvent.objects.filter(action="authorization.station_admin_granted").exists()
    client.force_login(station_admin)
    assert client.get(reverse("portal-station-manage", args=(station.id,))).status_code == 200


@pytest.mark.django_db
def test_department_admin_cannot_grant_cross_department_station_scope(client, portal_data):
    _, department_admin, _, outsider, _, _, _ = portal_data
    other_department = Department.objects.create(
        name="Other", short_code="OTHER", created_by=department_admin
    )
    other_station = Station.objects.create(
        department=other_department, name="Other station", short_code="OTH"
    )
    client.force_login(department_admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    response = client.post(
        reverse("portal-department-manage", args=(other_department.id,)),
        {"action": "grant-station", "user_id": outsider.id, "station_id": other_station.id},
    )
    assert response.status_code == 403
