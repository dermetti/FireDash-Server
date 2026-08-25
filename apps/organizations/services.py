from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment, TabletVehicleAssignment
from apps.assignments.services import AssignmentError
from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.organizations.models import Station, Vehicle
from apps.personnel.models import Person
from apps.publications.manifests import revoke_dataset_key_grants
from apps.tablets.models import AppInstallation, Tablet


@transaction.atomic
def create_station(
    *,
    actor,
    department,
    name: str,
    short_code: str,
    street: str = "",
    house_number: str = "",
    postal_code: str = "",
    city: str = "",
    active: bool = True,
) -> Station:
    require_department_admin(actor, department)
    station = Station.objects.create(
        department=department,
        name=name.strip(),
        short_code=short_code.strip(),
        street=street.strip(),
        house_number=house_number.strip(),
        postal_code=postal_code.strip(),
        city=city.strip(),
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
    *,
    actor,
    station: Station,
    name: str,
    short_code: str,
    street: str = "",
    house_number: str = "",
    postal_code: str = "",
    city: str = "",
    active: bool,
) -> Station:
    require_department_admin(actor, station.department)
    if not active and station.active:
        deactivate_station(station=station)
    else:
        station.name, station.short_code, station.active = (
            name.strip(),
            short_code.strip(),
            active,
        )
        station.street = street.strip()
        station.house_number = house_number.strip()
        station.postal_code = postal_code.strip()
        station.city = city.strip()
        station.save(
            update_fields=(
                "name",
                "short_code",
                "street",
                "house_number",
                "postal_code",
                "city",
                "active",
                "updated_at",
            )
        )
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
    active: bool = True,
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
        active=active,
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
        retire_vehicle(actor=actor, vehicle=vehicle)
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
def retire_vehicle(*, actor, vehicle: Vehicle) -> Vehicle:
    """Terminally retire a Vehicle and end its open Tablet assignments.

    Lock ordering is Vehicle, then Tablet (ascending primary key), then the
    corresponding assignment. Reassignment locks the Tablet before its open
    assignment as well, preventing a retirement/reassignment deadlock.
    """
    vehicle = (
        Vehicle.objects.select_for_update()
        .select_related("department", "station")
        .get(pk=vehicle.pk)
    )
    require_department_admin(actor, vehicle.department)
    if not vehicle.active:
        raise AssignmentError("Vehicle is already retired.")
    now = timezone.now()
    tablet_ids = list(
        TabletVehicleAssignment.objects.filter(
            vehicle=vehicle, valid_until__isnull=True, ended_at__isnull=True
        )
        .order_by("tablet_id")
        .values_list("tablet_id", flat=True)
    )
    for tablet_id in tablet_ids:
        tablet = Tablet.objects.select_for_update().get(pk=tablet_id)
        assignment = (
            TabletVehicleAssignment.objects.select_for_update()
            .filter(
                tablet=tablet,
                vehicle=vehicle,
                valid_until__isnull=True,
                ended_at__isnull=True,
            )
            .first()
        )
        if assignment is None:
            continue
        assignment.valid_until = now
        assignment.ended_at = now
        assignment.ended_by = actor
        assignment.end_reason = TabletVehicleAssignment.EndReason.VEHICLE_RETIRED
        assignment.save(update_fields=("valid_until", "ended_at", "ended_by", "end_reason"))
        for installation in AppInstallation.objects.select_for_update().filter(
            tablet=tablet,
            status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE),
        ):
            revoke_dataset_key_grants(installation=installation)
        record_event(
            action="tablet.vehicle_assignment_ended_vehicle_retired",
            actor_user=actor,
            department=vehicle.department,
            station=vehicle.station,
            target_type="tablet_vehicle_assignment",
            target_uuid=assignment.id,
            metadata={"tablet_id": str(tablet.id), "vehicle_id": str(vehicle.id)},
        )
    vehicle.active = False
    vehicle.save(update_fields=("active", "updated_at"))
    record_event(
        action="organization.vehicle_retired",
        actor_user=actor,
        department=vehicle.department,
        station=vehicle.station,
        target_type="vehicle",
        target_uuid=vehicle.id,
    )
    return vehicle


@transaction.atomic
def delete_station(*, actor, station: Station) -> None:
    """Permanently remove an erroneous empty station; never delete dependents."""
    station = Station.objects.select_for_update().select_related("department").get(pk=station.pk)
    require_department_admin(actor, station.department)
    station_id = station.id
    station_name = station.name
    station_short_code = station.short_code
    department = station.department
    try:
        station.delete()
    except ProtectedError as error:
        raise AssignmentError(
            "Station cannot be deleted while protected resources or history exist."
        ) from error
    record_event(
        action="organization.station_deleted",
        actor_user=actor,
        department=department,
        target_type="station",
        target_uuid=station_id,
        metadata={"name": station_name, "short_code": station_short_code},
    )


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
