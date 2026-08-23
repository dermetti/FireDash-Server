"""Stage 2 tablet assignment regression tests (department vs station admin)."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.assignments.services import AssignmentError, assign_tablet_vehicle
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import Tablet


@pytest.fixture
def assignment_scope(db):
    admin = User.objects.create_user("assign@example.test", "Assign", "safe-password")
    station_admin = User.objects.create_user("sa@example.test", "SA", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other = Department.objects.create(name="Bravo", short_code="BRV", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station_a = Station.objects.create(department=department, name="A", short_code="SA")
    station_b = Station.objects.create(department=department, name="B", short_code="SB")
    other_station = Station.objects.create(department=other, name="Bravo", short_code="BS")
    vehicle_a = Vehicle.objects.create(department=department, station=station_a, display_name="A1")
    vehicle_b = Vehicle.objects.create(department=department, station=station_b, display_name="B1")
    other_vehicle = Vehicle.objects.create(
        department=other, station=other_station, display_name="Bravo1"
    )
    StationAdminAssignment.objects.create(user=station_admin, station=station_a, created_by=admin)
    return SimpleNamespace(
        admin=admin,
        station_admin=station_admin,
        department=department,
        other=other,
        station_a=station_a,
        station_b=station_b,
        vehicle_a=vehicle_a,
        vehicle_b=vehicle_b,
        other_vehicle=other_vehicle,
    )


def _tablet(department, name="Tablet"):
    return Tablet.objects.create(department=department, display_name=name)


def _current_vehicle(tablet):
    assignment = tablet.vehicle_assignments.filter(
        valid_until__isnull=True, ended_at__isnull=True
    ).first()
    return assignment.vehicle if assignment else None


@pytest.mark.django_db
def test_department_admin_inter_station_move_succeeds(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_b, actor=scope.admin)
    assert _current_vehicle(tablet) == scope.vehicle_b
    assert AuditEvent.objects.filter(action="tablet.vehicle_assigned").exists()


@pytest.mark.django_db
def test_department_admin_cross_department_target_rejected(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    with pytest.raises(AssignmentError):
        assign_tablet_vehicle(tablet=tablet, vehicle=scope.other_vehicle, actor=scope.admin)


@pytest.mark.django_db
def test_station_admin_intra_station_move_succeeds(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    # A second vehicle in the same station is required for an intra-station move.
    vehicle_a2 = Vehicle.objects.create(
        department=scope.department, station=scope.station_a, display_name="A2"
    )
    assign_tablet_vehicle(tablet=tablet, vehicle=vehicle_a2, actor=scope.station_admin)
    assert _current_vehicle(tablet) == vehicle_a2


@pytest.mark.django_db
def test_station_admin_cannot_move_to_another_station(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    with pytest.raises(PermissionDenied):
        assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_b, actor=scope.station_admin)


@pytest.mark.django_db
def test_station_admin_cannot_pull_tablet_from_another_station(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_b, actor=scope.admin)
    # Target is in the station admin's station, but the source is not.
    with pytest.raises(PermissionDenied):
        assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.station_admin)


@pytest.mark.django_db
def test_station_admin_assignment_is_audited(assignment_scope):
    scope = assignment_scope
    tablet = _tablet(scope.department)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    vehicle_a2 = Vehicle.objects.create(
        department=scope.department, station=scope.station_a, display_name="A2"
    )
    assign_tablet_vehicle(tablet=tablet, vehicle=vehicle_a2, actor=scope.station_admin)
    assert AuditEvent.objects.filter(
        action="tablet.vehicle_assigned", actor_user=scope.station_admin
    ).exists()
