from django.db import transaction
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment, TabletVehicleAssignment
from apps.assignments.services import AssignmentError
from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.organizations.models import Station, Vehicle
from apps.personnel.models import Person
from apps.tablets.models import Tablet


@transaction.atomic
def create_station(
    *, actor, department, name: str, short_code: str, address: str = "", active: bool = True
) -> Station:
    require_department_admin(actor, department)
    station = Station.objects.create(
        department=department,
        name=name.strip(),
        short_code=short_code.strip(),
        address=address.strip(),
        active=active,
    )
    record_event(
        action="organization.station_created",
        actor_user=actor,
        department=department,
        station=station,
        target_type="station",
        target_uuid=station.id,
    )
    return station


@transaction.atomic
def update_station(
    *, actor, station: Station, name: str, short_code: str, address: str, active: bool
) -> Station:
    require_department_admin(actor, station.department)
    if not active and station.active:
        deactivate_station(station=station)
    else:
        station.name, station.short_code, station.address, station.active = (
            name.strip(),
            short_code.strip(),
            address.strip(),
            active,
        )
        station.save(update_fields=("name", "short_code", "address", "active", "updated_at"))
    record_event(
        action="organization.station_updated",
        actor_user=actor,
        department=station.department,
        station=station,
        target_type="station",
        target_uuid=station.id,
    )
    return station


@transaction.atomic
def create_vehicle(
    *,
    actor,
    department,
    station: Station,
    display_name: str,
    call_sign: str = "",
    asset_identifier: str = "",
) -> Vehicle:
    require_department_admin(actor, department)
    if station.department_id != department.id or not station.active:
        raise AssignmentError("Vehicle station must be active and in the selected department.")
    vehicle = Vehicle.objects.create(
        department=department,
        station=station,
        display_name=display_name.strip(),
        call_sign=call_sign.strip(),
        asset_identifier=asset_identifier.strip(),
    )
    record_event(
        action="organization.vehicle_created",
        actor_user=actor,
        department=department,
        station=station,
        target_type="vehicle",
        target_uuid=vehicle.id,
    )
    return vehicle


@transaction.atomic
def update_vehicle(
    *,
    actor,
    vehicle: Vehicle,
    display_name: str,
    call_sign: str,
    asset_identifier: str,
    active: bool,
) -> Vehicle:
    require_department_admin(actor, vehicle.department)
    if not active and vehicle.active:
        deactivate_vehicle(vehicle=vehicle)
    else:
        vehicle.display_name, vehicle.call_sign, vehicle.asset_identifier, vehicle.active = (
            display_name.strip(),
            call_sign.strip(),
            asset_identifier.strip(),
            active,
        )
        vehicle.save(
            update_fields=("display_name", "call_sign", "asset_identifier", "active", "updated_at")
        )
    record_event(
        action="organization.vehicle_updated",
        actor_user=actor,
        department=vehicle.department,
        station=vehicle.station,
        target_type="vehicle",
        target_uuid=vehicle.id,
    )
    return vehicle


@transaction.atomic
def deactivate_vehicle(*, vehicle: Vehicle) -> None:
    vehicle = Vehicle.objects.select_for_update().get(pk=vehicle.pk)
    if TabletVehicleAssignment.objects.filter(
        vehicle=vehicle, valid_until__isnull=True, ended_at__isnull=True
    ).exists():
        raise AssignmentError("End current tablet assignments before deactivating a vehicle.")
    vehicle.active = False
    vehicle.save(update_fields=("active", "updated_at"))


@transaction.atomic
def deactivate_station(*, station: Station) -> None:
    station = Station.objects.select_for_update().get(pk=station.pk)
    if Vehicle.objects.filter(station=station, active=True).exists():
        raise AssignmentError("Deactivate or move active vehicles before deactivating a station.")
    if PersonnelStationAssignment.objects.filter(
        station=station, valid_until__isnull=True, ended_at__isnull=True
    ).exists():
        raise AssignmentError(
            "End or transfer current personnel assignments before deactivating a station."
        )
    station.active = False
    station.save(update_fields=("active", "updated_at"))


@transaction.atomic
def deactivate_person(*, person: Person, actor) -> None:
    person = Person.objects.select_for_update().get(pk=person.pk)
    now = timezone.now()
    PersonnelStationAssignment.objects.filter(
        person=person, valid_until__isnull=True, ended_at__isnull=True
    ).update(valid_until=now, ended_at=now, ended_by=actor)
    person.active = False
    person.save(update_fields=("active",))


@transaction.atomic
def deactivate_tablet(*, tablet: Tablet, actor) -> None:
    tablet = Tablet.objects.select_for_update().get(pk=tablet.pk)
    now = timezone.now()
    TabletVehicleAssignment.objects.filter(
        tablet=tablet, valid_until__isnull=True, ended_at__isnull=True
    ).update(valid_until=now, ended_at=now, ended_by=actor)
    tablet.active = False
    tablet.save(update_fields=("active",))
