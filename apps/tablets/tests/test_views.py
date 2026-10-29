import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station
from apps.tablets.models import Tablet


@pytest.fixture
def tablet_admin_data(db):
    department_admin = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    department = Department.objects.create(
        name="Alpha", short_code="ALP", created_by=department_admin
    )
    station = Station.objects.create(department=department, name="Alpha Station", short_code="AST")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    StationAdminAssignment.objects.create(
        user=station_admin, station=station, created_by=department_admin
    )
    return department_admin, station_admin, department


@pytest.mark.django_db
def test_tablet_list_filters_by_query_and_status(client, tablet_admin_data):
    department_admin, _, department = tablet_admin_data
    active = Tablet.objects.create(
        department=department,
        display_name="Command Tablet",
        asset_number="CMD-01",
        status=Tablet.Status.ACTIVE,
    )
    Tablet.objects.create(
        department=department,
        display_name="Reserve Tablet",
        asset_number="RSV-02",
        status=Tablet.Status.INACTIVE,
    )
    client.force_login(department_admin)

    response = client.get(
        reverse("tablet-list", args=(department.id,)), {"q": "CMD", "status": "ACTIVE"}
    )

    assert list(response.context["tablets"]) == [active]
