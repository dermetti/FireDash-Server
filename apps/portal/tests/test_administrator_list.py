"""Current-authority Administrator Accounts list regressions."""

from datetime import timedelta

import pytest
from django.contrib.auth import authenticate
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountSetupToken, User
from apps.accounts.services import consume_setup_token, create_setup_token_for_user
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.authorization.scopes import active_department_ids, active_station_ids
from apps.authorization.services import provision_department_admin, provision_station_admin
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
    # Other current station administrators remain visible in their own compact
    # rows; this assertion is about the Department Administrator's one row.
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


@pytest.mark.django_db
@pytest.mark.parametrize("station_scope", [False, True])
def test_pending_administrator_is_visible_scoped_and_cannot_authenticate(
    client, administrator_list_scope, station_scope
):
    admin, _, _, department, stations = administrator_list_scope
    email = "pending@example.test"
    raw_token = (
        provision_station_admin(
            actor=admin, station=stations["F2921"], email=email, display_name="Pending"
        )
        if station_scope
        else provision_department_admin(
            actor=admin, department=department, email=email, display_name="Pending"
        )
    )
    user = User.objects.get(email=email)
    assert not user.is_active and not user.has_usable_password()
    assert authenticate(email=email, password="password") is None
    assert not list(active_department_ids(user))
    assert not list(active_station_ids(user))

    scope = str(stations["F2921"].id) if station_scope else "department"
    response = _list_response(client, admin, department, q="pending", scope=scope)
    assert response.context["total_count"] == 1
    row = response.context["administrators"][0]
    assert row.administrator == user
    assert row.scope == ("F2921" if station_scope else "Department")
    assert row.lifecycle == "PENDING_SETUP"
    assert row.lifecycle_display == "Pending setup"
    assert b'badge text-bg-warning">Pending setup</span>' in response.content
    wrong_scope = "department" if station_scope else str(stations["F2921"].id)
    assert (
        _list_response(client, admin, department, q="pending", scope=wrong_scope).context[
            "total_count"
        ]
        == 0
    )

    other = Department.objects.create(name="Other", short_code="OTHER", created_by=admin)
    other_admin = User.objects.create_user("other@example.test", "Other", "password")
    DepartmentMembership.objects.create(user=other_admin, department=other, created_by=admin)
    assert _list_response(client, other_admin, other, q="pending").context["total_count"] == 0
    client.force_login(user)
    denied = client.get(reverse("portal-department-manage", args=(department.id,)))
    assert denied.status_code == 302
    assert denied.wsgi_request.user.is_anonymous

    # Only completing the existing setup flow activates the account and changes
    # the list's lifecycle label. Listing the account never grants authority.
    assert consume_setup_token(raw_token=raw_token, password="new-password") == user
    assert authenticate(email=email, password="new-password") == user
    response = _list_response(client, admin, department, q="pending", scope=scope)
    assert response.context["administrators"][0].lifecycle == "ACTIVE"
    assert b"Pending setup" not in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("station_scope", [False, True])
def test_pending_list_requires_unconsumed_setup_but_keeps_expired_pending_accounts(
    client, administrator_list_scope, station_scope
):
    admin, _, _, department, stations = administrator_list_scope
    if station_scope:
        provision_station_admin(
            actor=admin,
            station=stations["F2921"],
            email="pending@example.test",
            display_name="Pending",
        )
    else:
        provision_department_admin(
            actor=admin, department=department, email="pending@example.test", display_name="Pending"
        )
    user = User.objects.get(email="pending@example.test")
    token = AccountSetupToken.objects.get(user=user)
    token.expires_at = timezone.now() - timedelta(hours=1)
    token.save(update_fields=("expires_at",))
    assert _list_response(client, admin, department, q="pending").context["total_count"] == 1
    token.used_at = timezone.now()
    token.save(update_fields=("used_at",))
    assert _list_response(client, admin, department, q="pending").context["total_count"] == 0
    token.delete()
    assert _list_response(client, admin, department, q="pending").context["total_count"] == 0


@pytest.mark.django_db
def test_pending_administrators_participate_in_pagination_without_duplicate_rows(
    client, administrator_list_scope
):
    admin, _, _, department, _ = administrator_list_scope
    for index in range(98):
        user = User.objects.create_user(f"page{index:03}@example.test", "Page", "password")
        DepartmentMembership.objects.create(user=user, department=department, created_by=admin)
    provision_department_admin(
        actor=admin, department=department, email="zzpending@example.test", display_name="Pending"
    )
    pending = User.objects.get(email="zzpending@example.test")
    create_setup_token_for_user(user=pending, actor=admin)
    first = _list_response(client, admin, department)
    assert first.context["total_count"] == 101
    assert len(first.context["administrators"]) == 100
    second = _list_response(client, admin, department, page=2)
    assert second.context["page"].paginator.num_pages == 2
    assert [row.administrator for row in second.context["administrators"]] == [pending]
    assert b"Pending setup" in second.content
