import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.services import record_event
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.organizations.models import Department, Station
from apps.reference_data.models import Hydrant


@pytest.fixture
def phase1_scope(client, db):
    admin = User.objects.create_user("phase1@example.test", "Phase One", "password")
    other_admin = User.objects.create_user("phase1-other@example.test", "Other", "password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other_department = Department.objects.create(
        name="Bravo", short_code="BRV", created_by=other_admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(
        user=other_admin, department=other_department, created_by=other_admin
    )
    station = Station.objects.create(department=department, name="Alpha Station", short_code="A1")
    Station.objects.create(
        department=department, name="Retired Station", short_code="RET", active=False
    )
    Hydrant.objects.create(
        department=department,
        external_identifier="HYD-ALPHA",
        geometry=Point(10.1, 53.5, srid=4326),
    )
    client.force_login(admin)
    return admin, other_admin, department, other_department, station


@pytest.mark.django_db
def test_phase1_bootstrap_forms_data_hub_and_scoped_htmx_lists(client, phase1_scope):
    _, _, department, other_department, station = phase1_scope

    hub = client.get(reverse("portal-data-hub", args=(department.id,)))
    assert hub.status_code == 200

    station_list = client.get(reverse("portal-stations", args=(department.id,)))
    assert list(station_list.context["stations"]) == [station]
    filtered = client.get(
        reverse("portal-stations", args=(department.id,)),
        {"active": "all", "q": "Retired"},
        HTTP_HX_REQUEST="true",
    )
    assert filtered.status_code == 200
    assert [row.name for row in filtered.context["stations"]] == ["Retired Station"]
    assert client.get(reverse("portal-stations", args=(other_department.id,))).status_code == 403

    hydrant_list = client.get(
        reverse("reference-data-hydrants", args=(department.id,)), HTTP_HX_REQUEST="true"
    )
    assert hydrant_list.status_code == 200


@pytest.mark.django_db
def test_phase1_audit_details_are_safe_and_scope_authorized(client, phase1_scope):
    admin, other_admin, department, other_department, _ = phase1_scope
    event = record_event(
        action="phase1.safe_event",
        actor_user=admin,
        department=department,
        target_type="station",
        metadata={"secret": "must-not-render", "safe": "recorded"},
    )
    department_detail = client.get(
        reverse("portal-department-audit-detail", args=(department.id, event.id))
    )
    assert department_detail.status_code == 200
    body = department_detail.content.decode()
    assert "must-not-render" not in body
    assert (
        client.get(
            reverse("portal-department-audit-detail", args=(other_department.id, event.id))
        ).status_code
        == 403
    )

    system_user = User.objects.create_user("phase1-system@example.test", "System", "password")
    SystemRole.objects.create(user=system_user, created_by=system_user)
    client.force_login(system_user)
    system_detail = client.get(reverse("portal-system-audit-detail", args=(event.id,)))
    assert system_detail.status_code == 200
    assert "must-not-render" not in system_detail.content.decode()

    client.force_login(other_admin)
    assert client.get(reverse("portal-system-audit-detail", args=(event.id,))).status_code == 403
