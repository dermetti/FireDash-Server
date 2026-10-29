"""Stage 1 navigation/shell contract tests."""

import pytest
from django.test import RequestFactory

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.organizations.models import Department, Station
from apps.portal.views import _nav_context


@pytest.fixture
def nav_roles(db):
    system_admin = User.objects.create_user("system@example.test", "System", "safe-password")
    dept_admin = User.objects.create_user("dept@example.test", "Dept", "safe-password")
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    SystemRole.objects.create(user=system_admin)
    department = Department.objects.create(name="Dept", short_code="DEP", created_by=system_admin)
    DepartmentMembership.objects.create(
        user=dept_admin, department=department, created_by=system_admin
    )
    s1 = Station.objects.create(department=department, name="One", short_code="ONE")
    s2 = Station.objects.create(department=department, name="Two", short_code="TWO")
    StationAdminAssignment.objects.create(user=station_admin, station=s1, created_by=dept_admin)
    return system_admin, dept_admin, station_admin, department, s1, s2


def _ctx(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return _nav_context(request)


@pytest.mark.django_db
def test_system_admin_nav_has_no_department_operational_links(nav_roles):
    system_admin, _, _, _, _, _ = nav_roles
    context = _ctx(system_admin)
    assert context["nav_role"] == "system"


@pytest.mark.django_db
def test_department_admin_nav_is_department_wide_without_mode_selector(nav_roles):
    _, dept_admin, _, department, _, _ = nav_roles
    context = _ctx(dept_admin)
    assert context["nav_role"] == "department"
    assert context["nav_department"] == department


@pytest.mark.django_db
def test_station_admin_nav_resolves_single_station(nav_roles):
    _, _, station_admin, _, s1, _ = nav_roles
    context = _ctx(station_admin)
    assert context["nav_role"] == "station"
    assert context["nav_station"] == s1
    assert context["nav_station_ambiguous"] is False


@pytest.mark.django_db
def test_station_admin_multiple_assignments_fail_safely_in_nav(nav_roles):
    _, dept_admin, station_admin, _, s1, s2 = nav_roles
    StationAdminAssignment.objects.create(user=station_admin, station=s2, created_by=dept_admin)
    context = _ctx(station_admin)
    assert context["nav_role"] == "station"
    assert context["nav_station"] is None
    assert context["nav_station_ambiguous"] is True


@pytest.mark.django_db
def test_system_admin_not_demoted_by_department_membership(nav_roles):
    system_admin, _, _, department, _, _ = nav_roles
    DepartmentMembership.objects.create(
        user=system_admin, department=department, created_by=system_admin
    )
    context = _ctx(system_admin)
    assert context["nav_role"] == "system"


@pytest.mark.django_db
def test_department_admin_not_demoted_by_station_assignment(nav_roles):
    _, dept_admin, _, _, s1, _ = nav_roles
    StationAdminAssignment.objects.create(user=dept_admin, station=s1, created_by=dept_admin)
    context = _ctx(dept_admin)
    assert context["nav_role"] == "department"
    assert "nav_station" not in context
