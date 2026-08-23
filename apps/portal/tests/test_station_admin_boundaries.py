import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.personnel.services import create_person


@pytest.fixture
def station_admin_scope(db):
    department_admin = User.objects.create_user(
        "department@example.test", "Department Admin", "safe-password"
    )
    station_admin = User.objects.create_user(
        "station@example.test", "Station Admin", "safe-password"
    )
    department = Department.objects.create(
        name="Department", short_code="DEP", created_by=department_admin
    )
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    station_one = Station.objects.create(
        department=department, name="Station One", short_code="ONE"
    )
    station_two = Station.objects.create(
        department=department, name="Station Two", short_code="TWO"
    )
    # A Station Administrator administers exactly ONE station.
    StationAdminAssignment.objects.create(
        user=station_admin, station=station_one, created_by=department_admin
    )
    person_one = create_person(
        actor=department_admin,
        department=department,
        home_station=station_one,
        personnel_number="1",
        first_name="One",
        last_name="Person",
    )
    person_two = create_person(
        actor=department_admin,
        department=department,
        home_station=station_two,
        personnel_number="2",
        first_name="Two",
        last_name="Person",
    )
    vehicle = Vehicle.objects.create(
        department=department, station=station_one, display_name="Engine One"
    )
    return station_admin, department, station_one, station_two, person_one, person_two, vehicle


@pytest.mark.django_db
def test_station_admin_reaches_personnel_without_station_param(client, station_admin_scope):
    station_admin, department, station_one, _, person_one, _, _ = station_admin_scope
    client.force_login(station_admin)

    people_url = reverse("personnel-list", args=(department.id,))
    response = client.get(people_url)

    assert response.status_code == 200
    assert list(response.context["people"]) == [person_one]


@pytest.mark.django_db
def test_station_admin_cannot_view_other_station_personnel(client, station_admin_scope):
    station_admin, department, _, station_two, _, person_two, _ = station_admin_scope
    client.force_login(station_admin)

    response = client.get(reverse("personnel-detail", args=(department.id, person_two.id)))
    assert response.status_code == 404

    assert client.get(reverse("portal-station-manage", args=(station_two.id,))).status_code == 403


@pytest.mark.django_db
def test_station_admin_multiple_assignments_fail_safely(client, station_admin_scope):
    station_admin, department, station_one, station_two, _, _, _ = station_admin_scope
    StationAdminAssignment.objects.create(
        user=station_admin, station=station_two, created_by=station_admin
    )
    client.force_login(station_admin)

    response = client.get(reverse("personnel-list", args=(department.id,)))

    assert response.status_code == 403
    assert AuditEvent.objects.filter(action="authorization.station_admin_ambiguous_scope").exists()


@pytest.mark.django_db
def test_station_admin_eligibility_is_station_scoped(client, station_admin_scope):
    station_admin, department, station_one, station_two, person_one, person_two, _ = (
        station_admin_scope
    )
    client.force_login(station_admin)

    response = client.post(
        reverse("personnel-eligibility", args=(department.id, person_one.id)),
        {"eligible": "on", "station_id": station_one.id},
    )
    assert response.status_code == 302
    person_one.refresh_from_db()
    assert person_one.incident_commander_eligible is True

    # The other station's person is not visible at all.
    assert (
        client.get(reverse("personnel-detail", args=(department.id, person_two.id))).status_code
        == 404
    )


@pytest.mark.django_db
def test_station_admin_is_denied_department_wide_routes_and_personnel_mutations(
    client, station_admin_scope
):
    station_admin, department, station, _, person, _, vehicle = station_admin_scope
    client.force_login(station_admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()

    for url in (
        reverse("portal-system-departments"),
        reverse("portal-department-manage", args=(department.id,)),
        reverse("portal-stations", args=(department.id,)),
        reverse("portal-department-audit", args=(department.id,)),
        reverse("reference-data-hydrants", args=(department.id,)),
        reverse("reference-data-fire-plans", args=(department.id,)),
        reverse("publications-list", args=(department.id,)),
    ):
        assert client.get(url).status_code == 403

    assert (
        client.post(
            reverse("portal-station-manage", args=(station.id,)), {"action": "station"}
        ).status_code
        == 403
    )
    assert client.post(reverse("portal-vehicle-create", args=(station.id,)), {}).status_code == 403
    assert client.post(reverse("portal-vehicle-manage", args=(vehicle.id,)), {}).status_code == 403

    assert (
        client.post(
            reverse("personnel-list", args=(department.id,)),
            {"first_name": "New", "last_name": "Person", "home_station_id": station.id},
        ).status_code
        == 403
    )
    assert (
        client.post(
            reverse("personnel-detail", args=(department.id, person.id)),
            {"first_name": "Updated", "last_name": "Person"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            reverse("personnel-email", args=(department.id, person.id)),
            {"email": "person@example.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(reverse("personnel-offboard", args=(department.id, person.id))).status_code
        == 403
    )
    assert (
        client.post(reverse("personnel-anonymize", args=(department.id, person.id))).status_code
        == 403
    )


@pytest.mark.django_db
def test_vehicle_forms_persist_submitted_fields_and_active_state(client, station_admin_scope):
    _, department, station, _, _, _, _ = station_admin_scope
    department_admin = User.objects.create_user("vehicle@example.test", "Vehicle", "safe-password")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    client.force_login(department_admin)

    response = client.post(
        reverse("portal-vehicle-create", args=(station.id,)),
        {"display_name": " Engine 1 ", "call_sign": " E-1 ", "asset_identifier": " A-1 "},
    )

    assert response.status_code == 302
    vehicle = Vehicle.objects.get(station=station, display_name="Engine 1")
    assert (vehicle.call_sign, vehicle.asset_identifier, vehicle.active) == ("E-1", "A-1", True)

    response = client.post(
        reverse("portal-vehicle-edit", args=(vehicle.id,)),
        {
            "display_name": "Engine Updated",
            "call_sign": "E-2",
            "asset_identifier": "A-2",
        },
    )

    assert response.status_code == 302
    vehicle.refresh_from_db()
    assert (vehicle.display_name, vehicle.call_sign, vehicle.asset_identifier, vehicle.active) == (
        "Engine Updated",
        "E-2",
        "A-2",
        True,
    )


@pytest.mark.django_db
def test_department_admin_can_grant_and_revoke_station_scope(client, station_admin_scope):
    station_admin, department, _, _, _, _, _ = station_admin_scope
    department_admin = User.objects.create_user("scope@example.test", "Scope", "safe-password")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    additional_station = Station.objects.create(
        department=department, name="Station Three", short_code="THR"
    )
    client.force_login(department_admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()

    response = client.post(
        reverse("portal-department-manage", args=(department.id,)),
        {
            "action": "grant-station",
            "user_id": station_admin.id,
            "station_id": additional_station.id,
        },
    )

    assert response.status_code == 302
    assignment = StationAdminAssignment.objects.get(user=station_admin, station=additional_station)
    assert assignment.active is True

    response = client.post(
        reverse("portal-department-manage", args=(department.id,)),
        {"action": "revoke-station", "assignment_id": assignment.id},
    )

    assert response.status_code == 302
    assignment.refresh_from_db()
    assert assignment.active is False
    assert assignment.revoked_by == department_admin
