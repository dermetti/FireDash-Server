from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment, TabletVehicleAssignment
from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids, active_station_ids
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department, Station, Vehicle
from apps.personnel.models import Person
from apps.publications.services import mark_dirty
from apps.tablets.models import Tablet


class AssignmentError(ValueError):
    pass


def _now(value: datetime | None) -> datetime:
    return value or timezone.now()


def _validate_operational_department(department: Department) -> None:
    if department.status != Department.Status.ACTIVE:
        raise AssignmentError("Department is not operationally active.")


def _validate_person_station(person: Person, station: Station) -> None:
    _validate_operational_department(person.department)
    if not person.active or not station.active or person.department_id != station.department_id:
        raise AssignmentError(
            "Person and station must be active and belong to the same department."
        )


def _validate_tablet_vehicle(tablet: Tablet, vehicle: Vehicle) -> None:
    _validate_operational_department(tablet.department)
    if not tablet.active or not vehicle.active or tablet.department_id != vehicle.department_id:
        raise AssignmentError(
            "Tablet and vehicle must be active and belong to the same department."
        )
    if not vehicle.station.active:
        raise AssignmentError("Vehicle station is not operationally active.")


def _current_home(person: Person):
    now = timezone.now()
    return (
        PersonnelStationAssignment.objects.select_for_update()
        .filter(
            person=person,
            assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
            valid_from__lte=now,
            ended_at__isnull=True,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .first()
    )


@transaction.atomic
def create_person_with_home(
    *, department: Department, station: Station, actor, effective_at: datetime | None = None
) -> Person:
    if department.id != station.department_id:
        raise AssignmentError("Person and home station must belong to the same department.")
    _validate_operational_department(department)
    if not station.active:
        raise AssignmentError("Home station is not operationally active.")
    person = Person.objects.create(department=department)
    PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
        valid_from=_now(effective_at),
        created_by=actor,
    )
    ensure_current_home(person)
    return person


@transaction.atomic
def transfer_home(
    *, person: Person, station: Station, actor, effective_at: datetime | None = None
) -> PersonnelStationAssignment:
    person = Person.objects.select_for_update().select_related("department").get(pk=person.pk)
    require_department_admin(actor, person.department)
    _validate_person_station(person, station)
    effective = _now(effective_at)
    current = _current_home(person)
    if current is None:
        raise AssignmentError("Active people require one current HOME assignment before transfer.")
    if current.station_id == station.id:
        raise AssignmentError("Person already has this home station.")
    current.valid_until = effective
    current.ended_at = timezone.now()
    current.ended_by = actor
    current.save(update_fields=("valid_until", "ended_at", "ended_by"))
    assignment = PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
        valid_from=effective,
        created_by=actor,
    )
    ensure_current_home(person)
    mark_dirty(
        department=person.department,
        station=current.station,
        dataset_type_code="station_personnel",
        actor=actor,
    )
    mark_dirty(
        department=person.department,
        station=station,
        dataset_type_code="station_personnel",
        actor=actor,
    )
    record_event(
        action="personnel.home_station_transferred",
        actor_user=actor,
        department=person.department,
        station=station,
        target_type="person",
        target_uuid=person.id,
    )
    return assignment


@transaction.atomic
def create_temporary_assignment(
    *, person: Person, station: Station, actor, valid_until, reason: str = ""
) -> PersonnelStationAssignment:
    person = Person.objects.select_for_update().select_related("department").get(pk=person.pk)
    require_department_admin(actor, person.department)
    _validate_person_station(person, station)
    now = timezone.now()
    if valid_until is None or valid_until <= now:
        raise AssignmentError("Temporary assignments require a future expiry.")
    assignment = PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.TEMPORARY,
        valid_from=now,
        valid_until=valid_until,
        reason=reason.strip(),
        created_by=actor,
    )
    mark_dirty(
        department=person.department,
        station=station,
        dataset_type_code="station_personnel",
        actor=actor,
    )
    record_event(
        action="personnel.temporary_assignment_created",
        actor_user=actor,
        department=person.department,
        station=station,
        target_type="personnel_station_assignment",
        target_uuid=assignment.id,
    )
    return assignment


@transaction.atomic
def end_temporary_assignment(*, assignment: PersonnelStationAssignment, actor=None) -> None:
    assignment = (
        PersonnelStationAssignment.objects.select_for_update()
        .select_related("person__department", "station")
        .get(pk=assignment.pk)
    )
    if assignment.assignment_type != PersonnelStationAssignment.AssignmentType.TEMPORARY:
        raise AssignmentError("Only temporary assignments can be ended through this service.")
    if assignment.ended_at is not None:
        return
    if actor is not None:
        require_department_admin(actor, assignment.person.department)
    assignment.ended_at = timezone.now()
    assignment.ended_by = actor
    assignment.save(update_fields=("ended_at", "ended_by"))
    mark_dirty(
        department=assignment.person.department,
        station=assignment.station,
        dataset_type_code="station_personnel",
        actor=actor,
    )
    record_event(
        action="personnel.temporary_assignment_ended"
        if actor
        else "personnel.temporary_assignment_expired",
        actor_user=actor,
        department=assignment.person.department,
        station=assignment.station,
        target_type="personnel_station_assignment",
        target_uuid=assignment.id,
    )


def _authorize_tablet_vehicle_assignment(actor, tablet: Tablet, vehicle: Vehicle) -> None:
    """Authorize a tablet reassignment for department or station administrators.

    Department administrators may move a tablet anywhere within the department.
    Station administrators may only move a tablet between vehicles in their own
    fixed station: the target vehicle must be in their station, and the tablet's
    current open assignment (if any) must also be in that station. This is a
    server-side authorization boundary, not a UI-only filter.
    """
    if tablet.department_id in active_department_ids(actor):
        require_department_admin(actor, tablet.department)
        return
    station_ids = list(active_station_ids(actor))
    if not station_ids:
        raise PermissionDenied("Department administrator scope is required.")
    if vehicle.station_id not in station_ids:
        raise PermissionDenied("Station administrator scope is required.")
    current = (
        TabletVehicleAssignment.objects.filter(
            tablet=tablet, valid_until__isnull=True, ended_at__isnull=True
        )
        .select_related("vehicle")
        .first()
    )
    if current is not None and current.vehicle.station_id not in station_ids:
        raise PermissionDenied("Station administrator scope is required.")


@transaction.atomic
def assign_tablet_vehicle(
    *, tablet: Tablet, vehicle: Vehicle, actor, effective_at: datetime | None = None
) -> TabletVehicleAssignment:
    # Match vehicle retirement's Vehicle -> Tablet -> assignment lock order and
    # validate persisted operational state rather than a potentially stale
    # caller-held Vehicle instance.
    vehicle = (
        Vehicle.objects.select_for_update()
        .select_related("department", "station")
        .get(pk=vehicle.pk)
    )
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    _authorize_tablet_vehicle_assignment(actor, tablet, vehicle)
    _validate_tablet_vehicle(tablet, vehicle)
    current = (
        TabletVehicleAssignment.objects.select_for_update()
        .filter(tablet=tablet, valid_until__isnull=True, ended_at__isnull=True)
        .first()
    )
    if current is not None:
        current.valid_until = _now(effective_at)
        current.ended_at = timezone.now()
        current.ended_by = actor
        current.end_reason = TabletVehicleAssignment.EndReason.REASSIGNED
        current.save(update_fields=("valid_until", "ended_at", "ended_by", "end_reason"))
    assignment = TabletVehicleAssignment.objects.create(
        tablet=tablet,
        vehicle=vehicle,
        valid_from=_now(effective_at),
        created_by=actor,
    )
    record_event(
        action="tablet.vehicle_assigned",
        actor_user=actor,
        department=tablet.department,
        station=vehicle.station,
        target_type="tablet_vehicle_assignment",
        target_uuid=assignment.id,
        metadata={"tablet_id": str(tablet.id), "vehicle_id": str(vehicle.id)},
    )
    return assignment


def ensure_current_home(person: Person) -> None:
    if person.active and _current_home(person) is None:
        raise AssignmentError("Every active person must have exactly one current HOME assignment.")
