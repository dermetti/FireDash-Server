from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import create_setup_token
from apps.audit.services import record_event
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.authorization.scopes import active_department_ids, is_system_admin
from apps.organizations.models import Department, Station


def require_system_admin(actor) -> None:
    if not is_system_admin(actor):
        raise PermissionDenied("System administrator role is required.")


def classify_system_admin_state(roles: list[Any]) -> str:
    """Classify bootstrap system-administrator state from active SystemRole rows.

    Ambiguity (more than one active role) always wins over active/inactive.
    """
    if len(roles) == 0:
        return "none"
    if len(roles) > 1:
        return "multiple"
    return "active" if roles[0].user.is_active else "inactive"


def require_department_admin(actor, department: Department) -> None:
    if department.status != Department.Status.ACTIVE or department.id not in active_department_ids(
        actor
    ):
        raise PermissionDenied("Department administrator scope is required.")


@transaction.atomic
def create_department(*, actor, name: str, short_code: str) -> Department:
    require_system_admin(actor)
    department = Department.objects.create(
        name=name.strip(), short_code=short_code.strip(), created_by=actor
    )
    record_event(
        action="organization.department_created",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
    )
    return department


@transaction.atomic
def change_department_status(*, actor, department: Department, status: str) -> Department:
    require_system_admin(actor)
    if status not in Department.Status.values:
        raise ValueError("Invalid department status.")
    department.status = status
    department.save(update_fields=("status",))
    record_event(
        action="organization.department_status_changed",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
    )
    return department


@transaction.atomic
def provision_department_admin(
    *, actor, department: Department, email: str, display_name: str
) -> str:
    if not (is_system_admin(actor) or department.id in active_department_ids(actor)):
        raise PermissionDenied("Department administrator scope is required.")
    token, raw_token = create_setup_token(actor=actor, email=email, display_name=display_name)
    DepartmentMembership.objects.create(user=token.user, department=department, created_by=actor)
    record_event(
        action="authorization.department_admin_provisioned",
        actor_user=actor,
        department=department,
        target_type="user",
        target_uuid=token.user.id,
    )
    return raw_token


@transaction.atomic
def provision_station_admin(*, actor, station: Station, email: str, display_name: str) -> str:
    require_department_admin(actor, station.department)
    token, raw_token = create_setup_token(actor=actor, email=email, display_name=display_name)
    StationAdminAssignment.objects.create(user=token.user, station=station, created_by=actor)
    record_event(
        action="authorization.station_admin_provisioned",
        actor_user=actor,
        department=station.department,
        station=station,
        target_type="user",
        target_uuid=token.user.id,
    )
    return raw_token


@transaction.atomic
def revoke_department_admin(*, actor, membership: DepartmentMembership) -> None:
    require_department_admin(actor, membership.department)
    membership.active = False
    membership.revoked_at = timezone.now()
    membership.revoked_by = actor
    membership.save(update_fields=("active", "revoked_at", "revoked_by"))
    record_event(
        action="authorization.department_admin_revoked",
        actor_user=actor,
        department=membership.department,
        target_type="department_membership",
        target_uuid=membership.id,
    )


@transaction.atomic
def revoke_station_admin(*, actor, assignment: StationAdminAssignment) -> None:
    require_department_admin(actor, assignment.station.department)
    assignment.active = False
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = actor
    assignment.save(update_fields=("active", "revoked_at", "revoked_by"))
    record_event(
        action="authorization.station_admin_revoked",
        actor_user=actor,
        department=assignment.station.department,
        station=assignment.station,
        target_type="station_admin_assignment",
        target_uuid=assignment.id,
    )


@transaction.atomic
def grant_station_admin(*, actor, user, station: Station) -> StationAdminAssignment:
    require_department_admin(actor, station.department)
    assignment, created = StationAdminAssignment.objects.get_or_create(
        user=user,
        station=station,
        active=True,
        defaults={"created_by": actor},
    )
    if not created:
        return assignment
    record_event(
        action="authorization.station_admin_granted",
        actor_user=actor,
        department=station.department,
        station=station,
        target_type="station_admin_assignment",
        target_uuid=assignment.id,
    )
    return assignment
