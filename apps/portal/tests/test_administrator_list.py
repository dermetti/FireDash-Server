"""Current-authority Administrator Accounts list regressions."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station


@pytest.fixture
def administrator_list_scope(db):
    admin = User.objects.create_user("fhh@example.test", "FHH Admin", "password")
    station_admin = User.objects.create_user("olli@example.test", "olli", "password")
    historical = User.objects.create_user("history@example.test", "History", "password")
    department = Department.objects.create(name="Hamburg", short_code="FHH", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    stations = {
        code: Station.objects.create(department=department, name=f"Station {code}", short_code=code)
        for code in ("F25", "F2921", "F3000", "F4000", "F5000")
    }
    StationAdminAssignment.objects.create(
        user=station_admin, station=stations["F2921"], created_by=admin
    )
    now = timezone.now()
    StationAdminAssignment.objects.create(
        user=historical,
        station=stations["F4000"],
        status=StationAdminAssignment.Status.SUSPENDED,
        created_by=admin,
        suspended_at=now,
        suspended_by=admin,
    )
    StationAdminAssignment.objects.create(
        user=historical,
        station=stations["F5000"],
        status=StationAdminAssignment.Status.REVOKED,
        created_by=admin,
        revoked_at=now,
        revoked_by=admin,
    )
    return admin, station_admin, historical, department, stations


def _list_response(client, admin, department, **params):
    client.force_login(admin)
    return client.get(reverse("portal-department-manage", args=(department.id,)), params)


@pytest.mark.django_db
def test_department_admin_list_is_one_sparse_current_authority_row(
    client, administrator_list_scope
):
    admin, _, _, department, stations = administrator_list_scope
    now = timezone.now()
    StationAdminAssignment.objects.create(user=admin, station=stations["F2921"], created_by=admin)
    StationAdminAssignment.objects.create(
        user=admin,
        station=stations["F3000"],
        status=StationAdminAssignment.Status.SUSPENDED,
        created_by=admin,
        suspended_at=now,
        suspended_by=admin,
    )
    StationAdminAssignment.objects.create(
        user=admin,
        station=stations["F4000"],
        status=StationAdminAssignment.Status.REVOKED,
        created_by=admin,
        revoked_at=now,
        revoked_by=admin,
    )

    response = _list_response(client, admin, department)
    assert response.status_code == 200
    rows = [row for row in response.context["administrators"] if row.administrator == admin]
    assert len(rows) == 1
    assert rows[0].scope == "Department"
    assert rows[0].lifecycle == DepartmentMembership.Status.ACTIVE
    assert rows[0].station_assignment is None

    table = response.content.decode().split("<table", 1)[1].split("</table>", 1)[0]
    assert "Authority" not in table and "Station scope" not in table
    assert "Department Administrator" not in table
    assert "F2921" not in table and "F3000" not in table and "F4000" not in table
    assert "Grant station" not in table
    assert ">View<" not in table
    assert reverse("portal-administrator-detail", args=(department.id, admin.id)) in table


@pytest.mark.django_db
def test_station_admin_only_row_uses_current_short_code_and_omits_historical_users(
    client, administrator_list_scope
):
    admin, station_admin, historical, department, _ = administrator_list_scope
    response = _list_response(client, admin, department)
    rows = {row.administrator: row for row in response.context["administrators"]}

    assert rows[station_admin].scope == "F2921"
    assert rows[station_admin].lifecycle == StationAdminAssignment.Status.ACTIVE
    assert historical not in rows
    table = response.content.decode().split("<table", 1)[1].split("</table>", 1)[0]
    assert "Station Administrator" not in table
    assert "F4000" not in table and "F5000" not in table


@pytest.mark.django_db
def test_multiple_current_station_scopes_are_compact_deterministic_and_not_duplicated(
    client, administrator_list_scope
):
    admin, station_admin, _, department, stations = administrator_list_scope
    StationAdminAssignment.objects.create(
        user=station_admin, station=stations["F25"], created_by=admin
    )

    response = _list_response(client, admin, department)
    rows = [row for row in response.context["administrators"] if row.administrator == station_admin]
    assert len(rows) == 1
    assert rows[0].scope == "F25, F2921"
    assert response.context["total_count"] == 2


@pytest.mark.django_db
def test_scope_filter_uses_current_scope_and_department_overrides_station_scope(
    client, administrator_list_scope
):
    admin, station_admin, _, department, stations = administrator_list_scope
    StationAdminAssignment.objects.create(user=admin, station=stations["F2921"], created_by=admin)

    department_response = _list_response(client, admin, department, scope="department")
    assert [row.administrator for row in department_response.context["administrators"]] == [admin]

    station_response = _list_response(client, admin, department, scope=str(stations["F2921"].id))
    assert [row.administrator for row in station_response.context["administrators"]] == [
        station_admin
    ]
    assert "Scope" in station_response.content.decode()
    assert "Station scope" not in station_response.content.decode()


@pytest.mark.django_db
def test_administrator_detail_retains_historical_station_authority(
    client, administrator_list_scope
):
    admin, _, historical, department, _ = administrator_list_scope
    client.force_login(admin)
    response = client.get(
        reverse("portal-administrator-detail", args=(department.id, historical.id))
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "F4000" in content and "Suspended" in content
    assert "F5000" in content and "Revoked" in content
