import pytest

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.assignments.services import AssignmentError, assign_tablet_vehicle
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.organizations.services import retire_vehicle
from apps.portal.overview import department_attention
from apps.tablets.models import Tablet


@pytest.fixture
def retirement_scope(db):
    admin = User.objects.create_user("retire@example.test", "Retire", "password")
    department = Department.objects.create(name="Retirement", short_code="RET", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="One", short_code="ONE")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    replacement = Vehicle.objects.create(
        department=department, station=station, display_name="Replacement"
    )
    tablet = Tablet.objects.create(
        department=department,
        display_name="Tablet",
        status=Tablet.Status.ACTIVE,
        created_by=admin,
    )
    return admin, department, vehicle, replacement, tablet


@pytest.mark.django_db
def test_retirement_ends_assignment_without_changing_tablet(retirement_scope):
    admin, department, vehicle, _, tablet = retirement_scope
    assignment = TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=tablet.created_at, created_by=admin
    )

    retire_vehicle(actor=admin, vehicle=vehicle)

    vehicle.refresh_from_db()
    tablet.refresh_from_db()
    assignment.refresh_from_db()
    assert not vehicle.active
    assert tablet.status == Tablet.Status.ACTIVE
    assert assignment.end_reason == TabletVehicleAssignment.EndReason.VEHICLE_RETIRED
    assert assignment.ended_at is not None
    assert any("unassigned" in item.text for item in department_attention(department))
    assert AuditEvent.objects.filter(action="organization.vehicle_retired").exists()


@pytest.mark.django_db
def test_reassignment_creates_new_history_and_clears_attention(retirement_scope):
    admin, department, vehicle, replacement, tablet = retirement_scope
    original = TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=tablet.created_at, created_by=admin
    )
    retire_vehicle(actor=admin, vehicle=vehicle)

    replacement_assignment = assign_tablet_vehicle(tablet=tablet, vehicle=replacement, actor=admin)

    original.refresh_from_db()
    assert original.end_reason == TabletVehicleAssignment.EndReason.VEHICLE_RETIRED
    assert replacement_assignment.pk != original.pk
    assert replacement_assignment.valid_until is None and replacement_assignment.ended_at is None
    assert not any("unassigned" in item.text for item in department_attention(department))
    assert AuditEvent.objects.filter(action="tablet.vehicle_assigned").exists()


@pytest.mark.django_db
def test_retired_vehicle_cannot_receive_reassignment(retirement_scope):
    admin, _, vehicle, _, tablet = retirement_scope
    retire_vehicle(actor=admin, vehicle=vehicle)
    with pytest.raises(AssignmentError):
        assign_tablet_vehicle(tablet=tablet, vehicle=vehicle, actor=admin)
