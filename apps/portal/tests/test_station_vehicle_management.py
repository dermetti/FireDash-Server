import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import Tablet


@pytest.fixture
def station_vehicle_scope(client, db):
    admin = User.objects.create_user("station-ui@example.test", "Admin", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Bravo", short_code="BRV")
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="HLF 1", call_sign="F 1"
    )
    other_department = Department.objects.create(name="Two", short_code="TWO", created_by=admin)
    other_station = Station.objects.create(
        department=other_department, name="Other", short_code="OTH"
    )
    other_vehicle = Vehicle.objects.create(
        department=other_department, station=other_station, display_name="Other HLF"
    )
    client.force_login(admin)
    return {
        "admin": admin,
        "department": department,
        "station": station,
        "vehicle": vehicle,
        "other_station": other_station,
        "other_vehicle": other_vehicle,
    }


def _station_data(**overrides):
    data = {
        "name": "Changed station",
        "short_code": "CHG",
        "street": "Example street",
        "house_number": "1",
        "postal_code": "20000",
        "city": "Hamburg",
    }
    data.update(overrides)
    return data


def _vehicle_data(**overrides):
    data = {"display_name": "New HLF", "call_sign": "F 2", "asset_identifier": "A-2"}
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_station_list_is_bounded_ordered_paginated_and_scoped(client, station_vehicle_scope):
    department = station_vehicle_scope["department"]
    Station.objects.bulk_create(
        [
            Station(department=department, name=f"Station {number:03}", short_code=f"S{number}")
            for number in range(101)
        ]
    )

    response = client.get(reverse("portal-stations", args=(department.id,)))

    assert response.status_code == 200
    assert len(response.context["stations"]) == 100
    expected = list(
        Station.objects.filter(department=department)
        .order_by("-active", "name", "short_code", "id")
        .values_list("id", flat=True)[:100]
    )
    assert list(response.context["stations"].values_list("id", flat=True)) == expected
    body = response.content.decode()
    assert str(station_vehicle_scope["other_station"].id) not in body


@pytest.mark.django_db
def test_station_edit_modal_validation_success_audit_and_department_boundary(
    client, station_vehicle_scope
):
    station = station_vehicle_scope["station"]
    edit_url = reverse("portal-station-edit", args=(station.id,))

    get_response = client.get(edit_url, HTTP_HX_REQUEST="true")
    assert get_response.status_code == 200

    invalid = client.post(
        edit_url, _station_data(name="", city="Invalid city"), HTTP_HX_REQUEST="true"
    )
    assert invalid.status_code == 200
    station.refresh_from_db()
    assert station.name == "Bravo"

    valid = client.post(edit_url, _station_data())
    assert valid.status_code == 302
    station.refresh_from_db()
    assert (station.name, station.short_code, station.city) == ("Changed station", "CHG", "Hamburg")
    assert AuditEvent.objects.filter(
        action="organization.station_updated", target_uuid=station.id
    ).exists()

    htmx_success = client.post(edit_url, _station_data(), HTTP_HX_REQUEST="true")
    assert htmx_success.status_code == 204
    assert htmx_success["HX-Redirect"] == reverse("portal-station-manage", args=(station.id,))

    forbidden = client.get(
        reverse("portal-station-edit", args=(station_vehicle_scope["other_station"].id,))
    )
    assert forbidden.status_code == 403


@pytest.mark.django_db
def test_station_delete_confirmation_success_and_protected_rollback(client, station_vehicle_scope):
    department = station_vehicle_scope["department"]
    safe_station = Station.objects.create(
        department=department, name="Disposable", short_code="DEL"
    )
    delete_url = reverse("portal-station-delete", args=(safe_station.id,))

    confirmation = client.get(delete_url, HTTP_HX_REQUEST="true")
    assert confirmation.status_code == 200
    assert Station.objects.filter(pk=safe_station.id).exists()

    deleted = client.post(delete_url)
    assert deleted.status_code == 302
    assert not Station.objects.filter(pk=safe_station.id).exists()
    assert AuditEvent.objects.filter(
        action="organization.station_deleted", target_uuid=safe_station.id
    ).exists()

    protected_station = station_vehicle_scope["station"]
    protected = client.post(reverse("portal-station-delete", args=(protected_station.id,)))
    assert protected.status_code == 200
    assert Station.objects.filter(pk=protected_station.id).exists()
    assert not AuditEvent.objects.filter(
        action="organization.station_deleted", target_uuid=protected_station.id
    ).exists()


@pytest.mark.django_db
def test_vehicle_create_modal_validation_success_audit_and_department_boundary(
    client, station_vehicle_scope
):
    station = station_vehicle_scope["station"]
    create_url = reverse("portal-vehicle-create", args=(station.id,))

    get_response = client.get(create_url, HTTP_HX_REQUEST="true")
    assert get_response.status_code == 200

    invalid = client.post(create_url, _vehicle_data(display_name="", call_sign="Entered"))
    assert invalid.status_code == 200
    assert not Vehicle.objects.filter(department=station.department, call_sign="Entered").exists()

    valid = client.post(create_url, _vehicle_data())
    assert valid.status_code == 302
    vehicle = Vehicle.objects.get(department=station.department, display_name="New HLF")
    assert vehicle.station_id == station.id and vehicle.active is True
    assert AuditEvent.objects.filter(
        action="organization.vehicle_created", target_uuid=vehicle.id
    ).exists()

    forbidden = client.get(
        reverse("portal-vehicle-create", args=(station_vehicle_scope["other_station"].id,))
    )
    assert forbidden.status_code == 403


@pytest.mark.django_db
def test_vehicle_edit_modal_validation_success_audit_and_department_boundary(
    client, station_vehicle_scope
):
    vehicle = station_vehicle_scope["vehicle"]
    edit_url = reverse("portal-vehicle-edit", args=(vehicle.id,))

    get_response = client.get(edit_url, HTTP_HX_REQUEST="true")
    assert get_response.status_code == 200

    invalid = client.post(edit_url, _vehicle_data(display_name="", call_sign="Changed call sign"))
    assert invalid.status_code == 200
    vehicle.refresh_from_db()
    assert vehicle.display_name == "HLF 1"

    valid = client.post(edit_url, _vehicle_data(display_name="Updated HLF"))
    assert valid.status_code == 302
    vehicle.refresh_from_db()
    assert (vehicle.display_name, vehicle.call_sign, vehicle.asset_identifier) == (
        "Updated HLF",
        "F 2",
        "A-2",
    )
    assert AuditEvent.objects.filter(
        action="organization.vehicle_updated", target_uuid=vehicle.id
    ).exists()

    htmx_success = client.post(
        edit_url, _vehicle_data(display_name="Updated HLF"), HTTP_HX_REQUEST="true"
    )
    assert htmx_success.status_code == 204
    assert htmx_success["HX-Redirect"] == reverse("portal-vehicle-manage", args=(vehicle.id,))

    forbidden = client.get(
        reverse("portal-vehicle-edit", args=(station_vehicle_scope["other_vehicle"].id,))
    )
    assert forbidden.status_code == 403


@pytest.mark.django_db
def test_vehicle_retirement_ends_assignment_and_preserves_tablet(client, station_vehicle_scope):
    admin = station_vehicle_scope["admin"]
    station = station_vehicle_scope["station"]
    vehicle = station_vehicle_scope["vehicle"]

    tablet = Tablet.objects.create(
        department=station.department, display_name="Assigned tablet", created_by=admin
    )
    assignment = TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=admin
    )
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    retire_url = reverse("portal-vehicle-retire", args=(vehicle.id,))
    confirmation = client.get(retire_url, HTTP_HX_REQUEST="true")
    assert confirmation.status_code == 200
    retired = client.post(retire_url, HTTP_HX_REQUEST="true")
    assert retired.status_code == 204
    vehicle.refresh_from_db()
    tablet.refresh_from_db()
    assignment.refresh_from_db()
    assert vehicle.active is False
    assert tablet.status == Tablet.Status.INACTIVE
    assert assignment.ended_at is not None and assignment.valid_until is not None
    assert assignment.end_reason == TabletVehicleAssignment.EndReason.VEHICLE_RETIRED
    assert Vehicle.objects.filter(pk=vehicle.id).exists()
    assert AuditEvent.objects.filter(
        action="organization.vehicle_retired", target_uuid=vehicle.id
    ).exists()
    assert AuditEvent.objects.filter(
        action="tablet.vehicle_assignment_ended_vehicle_retired", target_uuid=assignment.id
    ).exists()


@pytest.mark.django_db
def test_station_detail_vehicle_actions_legacy_redirect_and_navigation(
    client, station_vehicle_scope
):
    station = station_vehicle_scope["station"]
    vehicle = station_vehicle_scope["vehicle"]
    detail = client.get(reverse("portal-station-manage", args=(station.id,)))
    body = detail.content.decode()
    assert detail.status_code == 200
    assert station_vehicle_scope["other_vehicle"].display_name not in body

    legacy = client.get(reverse("portal-vehicles", args=(station.id,)))
    assert legacy.status_code == 302
    assert legacy.url == reverse("portal-station-manage", args=(station.id,))

    vehicle_detail = client.get(reverse("portal-vehicle-manage", args=(vehicle.id,)))
    assert vehicle_detail.status_code == 200


@pytest.mark.django_db
def test_gets_do_not_mutate_and_csrf_remains_enforced(client, station_vehicle_scope):
    admin = station_vehicle_scope["admin"]
    station = station_vehicle_scope["station"]
    vehicle = station_vehicle_scope["vehicle"]
    original_name = station.name
    original_vehicle_name = vehicle.display_name

    for url in (
        reverse("portal-station-edit", args=(station.id,)),
        reverse("portal-station-delete", args=(station.id,)),
        reverse("portal-vehicle-create", args=(station.id,)),
        reverse("portal-vehicle-edit", args=(vehicle.id,)),
        reverse("portal-vehicle-retire", args=(vehicle.id,)),
    ):
        assert client.get(url).status_code == 200
    station.refresh_from_db()
    vehicle.refresh_from_db()
    assert station.name == original_name and vehicle.display_name == original_vehicle_name

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(admin)
    blocked = csrf_client.post(reverse("portal-station-edit", args=(station.id,)), _station_data())
    assert blocked.status_code == 403
    station.refresh_from_db()
    assert station.name == original_name
