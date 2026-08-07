import pytest
from django.urls import reverse

from apps.accounts.models import User
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
def test_system_pages_are_system_admin_only(client, portal_data):
    system_admin, department_admin, _, _, department, _, _ = portal_data
    client.force_login(system_admin)
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
