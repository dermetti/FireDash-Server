"""Stage 1 navigation/shell contract tests."""

import pytest
from django.test import RequestFactory
from django.urls import reverse

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


def _child_labels(context):
    return [
        item["label"] for section in context["nav_sections"] for item in section.get("children", [])
    ]


def _all_labels(context):
    labels = []
    for section in context["nav_sections"]:
        labels.append(section["label"])
        labels.extend(item["label"] for item in section.get("children", []))
    return labels


@pytest.mark.django_db
def test_system_admin_nav_has_no_department_operational_links(nav_roles):
    system_admin, _, _, _, _, _ = nav_roles
    context = _ctx(system_admin)
    assert context["nav_role"] == "system"
    labels = _all_labels(context)
    assert "Departments" in labels
    assert "Personnel" not in labels
    assert "Tablets" not in labels
    assert "Stations" not in labels


@pytest.mark.django_db
def test_department_admin_nav_is_department_wide_without_mode_selector(nav_roles):
    _, dept_admin, _, department, _, _ = nav_roles
    context = _ctx(dept_admin)
    assert context["nav_role"] == "department"
    assert context["nav_department"] == department
    labels = _all_labels(context)
    assert "Data Hub" in labels
    assert "Publications" in labels
    assert "Tablets" in labels
    assert "Stations" in labels
    assert "Administrator Accounts" in labels
    assert "System Settings" in labels
    assert "Audit Logs" in labels
    for removed_label in (
        "Personnel",
        "Hydrants",
        "Fire Plans",
        "KLGV Plans",
        "Vehicles",
        "Imports",
    ):
        assert removed_label not in labels
    # No station authorization-mode selector state.
    assert "nav_mode" not in context
    assert "nav_stations" not in context
    assert "nav_departments" not in context


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
    assert "Departments" in _all_labels(context)


@pytest.mark.django_db
def test_department_admin_not_demoted_by_station_assignment(nav_roles):
    _, dept_admin, _, _, s1, _ = nav_roles
    StationAdminAssignment.objects.create(user=dept_admin, station=s1, created_by=dept_admin)
    context = _ctx(dept_admin)
    assert context["nav_role"] == "department"
    assert "nav_station" not in context


@pytest.mark.django_db
def test_nav_has_no_blank_labels_or_urls(nav_roles):
    for user in nav_roles[:3]:
        context = _ctx(user)
        for section in context["nav_sections"]:
            assert section["label"]
            if "url" in section:
                assert section["url"]
            for item in section.get("children", []):
                assert item["label"]
                assert item["url"]


@pytest.mark.django_db
def test_authenticated_pages_render_shared_shell(client, nav_roles):
    _, dept_admin, _, department, _, _ = nav_roles
    client.force_login(dept_admin)
    response = client.get(reverse("tablet-list", args=(department.id,)))
    assert response.status_code == 200
    content = response.content.decode()
    # Desktop sidebar + mobile Offcanvas are both present.
    assert 'id="navOffcanvas"' in content
    assert "Distributed Data" in content
    # The old station scope-switch selector is gone.
    assert 'onchange="window.location=this.value"' not in content


@pytest.mark.django_db
def test_rendered_navigation_has_no_blank_links(client, nav_roles):
    _, dept_admin, _, department, _, _ = nav_roles
    client.force_login(dept_admin)
    response = client.get(reverse("tablet-list", args=(department.id,)))
    content = response.content.decode()
    assert 'href=""' not in content
    # The single shared navigation source is rendered by both the desktop
    # sidebar and the Offcanvas.
    assert content.count("Distributed Data") >= 2
