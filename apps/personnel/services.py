from datetime import timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.assignments.services import ensure_current_home
from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids, active_station_ids
from apps.organizations.models import Department, Station
from apps.personnel.models import Person, PersonnelRetentionPolicy
from apps.publications.services import mark_dirty


class PersonnelError(ValueError):
    pass


def visible_to_user(*, user, department_id):
    if department_id in active_department_ids(user):
        return Person.objects.filter(department_id=department_id)
    station_ids = active_station_ids(user)
    if not Station.objects.filter(id__in=station_ids, department_id=department_id).exists():
        return Person.objects.none()
    department_people = Person.objects.filter(department_id=department_id)
    now = timezone.now()
    assigned_people = department_people.filter(
        station_assignments__station_id__in=station_ids,
        station_assignments__valid_from__lte=now,
        station_assignments__ended_at__isnull=True,
    ).filter(
        Q(station_assignments__valid_until__isnull=True)
        | Q(station_assignments__valid_until__gt=now)
    )
    return assigned_people.distinct()


def _is_department_admin(user, department_id) -> bool:
    return department_id in active_department_ids(user)


def _has_home_station_scope(user, person: Person) -> bool:
    now = timezone.now()
    return (
        PersonnelStationAssignment.objects.filter(
            person=person,
            assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
            station_id__in=active_station_ids(user),
            valid_from__lte=now,
            ended_at__isnull=True,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .exists()
    )


def _visible_station_ids(person: Person) -> list[UUID]:
    now = timezone.now()
    return list(
        PersonnelStationAssignment.objects.filter(
            person=person,
            station__active=True,
            valid_from__lte=now,
            ended_at__isnull=True,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .values_list("station_id", flat=True)
        .distinct()
    )


def _mark_visible_station_scopes(*, person: Person, actor) -> None:
    for station_id in _visible_station_ids(person):
        mark_dirty(
            department=person.department,
            station=Station.objects.get(pk=station_id),
            dataset_type_code="station_personnel",
            actor=actor,
        )


def _require_department_person_admin(user, person: Person) -> None:
    if not _is_department_admin(user, person.department_id):
        raise PermissionDenied("Department administrator scope is required.")


def _station_can_manage_visible_person(*, user, person: Person, station: Station) -> bool:
    now = timezone.now()
    return (
        station.id in active_station_ids(user)
        and PersonnelStationAssignment.objects.filter(
            person=person,
            station=station,
            valid_from__lte=now,
            ended_at__isnull=True,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .exists()
    )


def _normalize_email(value: str) -> str:
    return value.strip().casefold()


@transaction.atomic
def create_person(
    *,
    actor,
    department: Department,
    home_station: Station,
    personnel_number: str | None,
    first_name: str,
    last_name: str,
) -> Person:
    if not _is_department_admin(actor, department.id):
        raise PermissionDenied("Only department administrators can create personnel.")
    if department.id != home_station.department_id or not home_station.active:
        raise PersonnelError("Home station must be active and in the personnel department.")
    display_name = f"{first_name.strip()} {last_name.strip()}".strip()
    if not display_name:
        raise PersonnelError("Personnel display name is required.")
    person = Person.objects.create(
        department=department,
        personnel_number=personnel_number or None,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        display_name=display_name,
    )
    PersonnelStationAssignment.objects.create(
        person=person,
        station=home_station,
        assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
        valid_from=timezone.now(),
        created_by=actor,
    )
    ensure_current_home(person)
    record_event(
        action="personnel.created",
        actor_user=actor,
        department=department,
        station=home_station,
        target_type="person",
        target_uuid=person.id,
    )
    mark_dirty(
        department=department,
        station=home_station,
        dataset_type_code="station_personnel",
        actor=actor,
    )
    return person


@transaction.atomic
def update_person(
    *, actor, person: Person, personnel_number: str | None, first_name: str, last_name: str
) -> Person:
    person = Person.objects.select_for_update().get(pk=person.pk)
    _require_department_person_admin(actor, person)
    if person.lifecycle_status != Person.LifecycleStatus.ACTIVE:
        raise PersonnelError("Only active personnel can be edited.")
    person.personnel_number = personnel_number or None
    person.first_name = first_name.strip()
    person.last_name = last_name.strip()
    person.display_name = f"{person.first_name} {person.last_name}".strip()
    if not person.display_name:
        raise PersonnelError("Personnel display name is required.")
    person.save(
        update_fields=("personnel_number", "first_name", "last_name", "display_name", "updated_at")
    )
    record_event(
        action="personnel.updated",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    _mark_visible_station_scopes(person=person, actor=actor)
    return person


@transaction.atomic
def delete_person(*, actor, person: Person) -> None:
    """Permanently remove only an erroneous person with no protected history."""
    person = Person.objects.select_for_update().select_related("department").get(pk=person.pk)
    _require_department_person_admin(actor, person)
    affected_station_ids = _visible_station_ids(person)
    person_id = person.id
    department = person.department
    metadata = {
        "personnel_number": person.personnel_number,
        "display_name": person.display_name,
    }
    try:
        person.delete()
    except ProtectedError as error:
        raise PersonnelError(
            "Personnel cannot be deleted while protected assignment or history records exist."
        ) from error
    record_event(
        action="personnel.deleted",
        actor_user=actor,
        department=department,
        target_type="person",
        target_uuid=person_id,
        metadata=metadata,
    )
    for station_id in affected_station_ids:
        mark_dirty(
            department=department,
            station=Station.objects.get(pk=station_id),
            dataset_type_code="station_personnel",
            actor=actor,
        )


@transaction.atomic
def set_commander_eligibility(
    *, actor, person: Person, eligible: bool, station: Station | None = None
) -> Person:
    person = Person.objects.select_for_update().get(pk=person.pk)
    if not _is_department_admin(actor, person.department_id) and not (
        station
        and station.department_id == person.department_id
        and _station_can_manage_visible_person(user=actor, person=person, station=station)
    ):
        raise PermissionDenied("Personnel is outside the administrator's scope.")
    if person.lifecycle_status != Person.LifecycleStatus.ACTIVE:
        raise PersonnelError("Only active personnel can be commander eligible.")
    person.incident_commander_eligible = eligible
    if not eligible:
        person.email_verified_at = None
        person.email_verified_by = None
    person.save(
        update_fields=(
            "incident_commander_eligible",
            "email_verified_at",
            "email_verified_by",
            "updated_at",
        )
    )
    record_event(
        action="personnel.commander_eligibility_changed",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    _mark_visible_station_scopes(person=person, actor=actor)
    return person


@transaction.atomic
def set_commander_email(*, actor, person: Person, email: str) -> Person:
    person = Person.objects.select_for_update().get(pk=person.pk)
    _require_department_person_admin(actor, person)
    if not person.incident_commander_eligible:
        raise PersonnelError("Commander eligibility is required before setting commander email.")
    person.incident_commander_email = _normalize_email(email)
    person.email_verified_at = None
    person.email_verified_by = None
    person.save(
        update_fields=(
            "incident_commander_email",
            "email_verified_at",
            "email_verified_by",
            "updated_at",
        )
    )
    record_event(
        action="personnel.commander_email_changed",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    _mark_visible_station_scopes(person=person, actor=actor)
    return person


@transaction.atomic
def verify_commander_email(*, actor, person: Person) -> Person:
    person = Person.objects.select_for_update().get(pk=person.pk)
    _require_department_person_admin(actor, person)
    if not person.incident_commander_eligible or not person.incident_commander_email:
        raise PersonnelError(
            "Eligible personnel with an email address are required for verification."
        )
    person.email_verified_at = timezone.now()
    person.email_verified_by = actor
    person.save(update_fields=("email_verified_at", "email_verified_by", "updated_at"))
    record_event(
        action="personnel.commander_email_verified",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    _mark_visible_station_scopes(person=person, actor=actor)
    return person


@transaction.atomic
def set_retention_policy(
    *, actor, department: Department, retention_period: timedelta
) -> PersonnelRetentionPolicy:
    if not _is_department_admin(actor, department.id) or retention_period <= timedelta(0):
        raise PermissionDenied("A positive retention period and department scope are required.")
    policy, _ = PersonnelRetentionPolicy.objects.update_or_create(
        department=department,
        defaults={"retention_period": retention_period, "updated_by": actor},
    )
    record_event(
        action="personnel.retention_policy_changed",
        actor_user=actor,
        department=department,
        target_type="personnel_retention_policy",
        target_uuid=department.id,
    )
    return policy


@transaction.atomic
def offboard_person(*, actor, person: Person) -> Person:
    person = Person.objects.select_for_update().select_related("department").get(pk=person.pk)
    if not _is_department_admin(actor, person.department_id):
        raise PermissionDenied("Only department administrators can offboard personnel.")
    policy = PersonnelRetentionPolicy.objects.filter(department=person.department).first()
    if policy is None:
        raise PersonnelError("A department retention policy is required before offboarding.")
    now = timezone.now()
    affected_station_ids = _visible_station_ids(person)
    PersonnelStationAssignment.objects.filter(
        person=person, ended_at__isnull=True, valid_until__isnull=True
    ).update(valid_until=now, ended_at=now, ended_by=actor)
    person.lifecycle_status = Person.LifecycleStatus.DEPARTED
    person.active = False
    person.departed_at = now
    person.retention_until = now + policy.retention_period
    person.incident_commander_eligible = False
    person.incident_commander_email = None
    person.email_verified_at = None
    person.email_verified_by = None
    person.save()
    record_event(
        action="personnel.offboarded",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    for station_id in affected_station_ids:
        mark_dirty(
            department=person.department,
            station=Station.objects.get(pk=station_id),
            dataset_type_code="station_personnel",
            actor=actor,
        )
    return person


@transaction.atomic
def anonymize_person(*, actor, person: Person) -> Person:
    person = Person.objects.select_for_update().select_related("department").get(pk=person.pk)
    if not _is_department_admin(actor, person.department_id):
        raise PermissionDenied("Only department administrators can anonymize personnel.")
    if (
        person.lifecycle_status != Person.LifecycleStatus.DEPARTED
        or not person.retention_until
        or person.retention_until > timezone.now()
    ):
        raise PersonnelError("Personnel retention period has not expired.")
    person.personnel_number = None
    person.first_name = None
    person.last_name = None
    person.display_name = "Former member"
    person.incident_commander_eligible = False
    person.incident_commander_email = None
    person.email_verified_at = None
    person.email_verified_by = None
    person.lifecycle_status = Person.LifecycleStatus.ANONYMIZED
    person.active = False
    person.anonymized_at = timezone.now()
    person.anonymized_by = actor
    person.save()
    record_event(
        action="personnel.anonymized",
        actor_user=actor,
        department=person.department,
        target_type="person",
        target_uuid=person.id,
    )
    _mark_visible_station_scopes(person=person, actor=actor)
    return person
