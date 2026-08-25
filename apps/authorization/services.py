from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import create_setup_token, permanently_deactivate_and_anonymize_user
from apps.audit.services import record_event
from apps.authorization.models import (
    ApiVersionCompatibilityPolicy,
    DepartmentMembership,
    StationAdminAssignment,
)
from apps.authorization.scopes import (
    active_department_ids,
    effective_department_admin_memberships,
    is_system_admin,
)
from apps.organizations.models import Department, Station
from apps.organizations.presentation import (
    DEPARTMENT_LOCALE_CHOICES,
    DEPARTMENT_TIMEZONE_CHOICES,
)
from apps.tablets.versions import AppVersionError, parse_app_version


def require_system_admin(actor) -> None:
    if not is_system_admin(actor):
        raise PermissionDenied("System administrator role is required.")


def minimum_supported_app_version(*, api_major: int):
    """Return the configured minimum for a server-selected API generation."""
    policy = (
        ApiVersionCompatibilityPolicy.objects.filter(api_major=api_major)
        .only("minimum_app_version")
        .first()
    )
    return (
        None
        if policy is None or policy.minimum_app_version is None
        else parse_app_version(policy.minimum_app_version)
    )


@transaction.atomic
def set_api_version_compatibility_policy(
    *, actor, api_major: int, minimum_app_version: str | None
) -> ApiVersionCompatibilityPolicy:
    require_system_admin(actor)
    if api_major < 1:
        raise ValueError("API major must be positive.")
    normalized = None
    if minimum_app_version:
        try:
            normalized = str(parse_app_version(minimum_app_version))
        except AppVersionError as error:
            raise ValueError(str(error)) from error
    policy, created = ApiVersionCompatibilityPolicy.objects.select_for_update().get_or_create(
        api_major=api_major,
        defaults={"minimum_app_version": normalized, "updated_by": actor},
    )
    old_value = policy.minimum_app_version
    policy.minimum_app_version = normalized
    policy.updated_by = actor
    policy.save(update_fields=("minimum_app_version", "updated_by", "updated_at"))
    if created or old_value != normalized:
        record_event(
            action="api_compatibility_policy.updated",
            actor_user=actor,
            target_type="api_version_compatibility_policy",
            target_uuid=None,
            metadata={"api_major": api_major, "old_minimum": old_value, "new_minimum": normalized},
        )
    return policy


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
def set_department_tablet_lease(
    *, actor, department: Department, tablet_lease_days: int
) -> Department:
    """Persist the existing per-department lease setting with an audit trail."""
    require_department_admin(actor, department)
    if tablet_lease_days < 3 or tablet_lease_days > 365:
        raise ValueError("Tablet lease duration must be between 3 and 365 days.")
    department = Department.objects.select_for_update().get(pk=department.pk)
    old_value = department.tablet_lease_days
    if old_value == tablet_lease_days:
        return department
    department.tablet_lease_days = tablet_lease_days
    department.save(update_fields=("tablet_lease_days",))
    record_event(
        action="authorization.department_tablet_lease_changed",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
        metadata={"old_days": old_value, "new_days": tablet_lease_days},
    )
    return department


@transaction.atomic
def set_department_tablet_asset_number_policy(
    *, actor, department: Department, auto_enabled: bool, prefix: str, width: int
) -> Department:
    """Update the Department-owned Tablet asset-number formatting policy.

    The persistent numeric sequence is intentionally not an administrator-editable
    preference.  Changing the optional prefix or minimum width only affects future
    formatting and never resets or reinterprets previously allocated values.
    """
    require_department_admin(actor, department)
    prefix = prefix.strip()
    if width < 1 or width > 20:
        raise ValueError("Tablet asset-number width must be between 1 and 20 digits.")

    # Keep the policy structurally compatible with the canonical Tablet identifier
    # field.  Allocation separately verifies that future numeric growth still fits.
    from apps.tablets.models import Tablet

    asset_number_max_length = Tablet._meta.get_field("asset_number").max_length
    if len(prefix) + width > asset_number_max_length:
        raise ValueError(
            "The prefix and number width must fit within the Tablet asset-number length."
        )

    department = Department.objects.select_for_update().get(pk=department.pk)
    old_values = {
        "auto_enabled": department.tablet_asset_number_auto_enabled,
        "prefix": department.tablet_asset_number_prefix,
        "width": department.tablet_asset_number_width,
    }
    new_values = {"auto_enabled": auto_enabled, "prefix": prefix, "width": width}
    if old_values == new_values:
        return department
    department.tablet_asset_number_auto_enabled = auto_enabled
    department.tablet_asset_number_prefix = prefix
    department.tablet_asset_number_width = width
    department.save(
        update_fields=(
            "tablet_asset_number_auto_enabled",
            "tablet_asset_number_prefix",
            "tablet_asset_number_width",
        )
    )
    record_event(
        action="authorization.department_tablet_asset_number_policy_changed",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
        metadata={
            "old_auto_enabled": old_values["auto_enabled"],
            "new_auto_enabled": new_values["auto_enabled"],
            "old_prefix": old_values["prefix"],
            "new_prefix": new_values["prefix"],
            "old_width": old_values["width"],
            "new_width": new_values["width"],
        },
    )
    return department


@transaction.atomic
def set_department_locale_time_policy(
    *, actor, department: Department, locale: str, timezone_name: str
) -> Department:
    """Update Department-local HTML presentation policy only.

    This deliberately does not activate a process-wide timezone or affect any
    protocol, signing, or security timestamp semantics.
    """
    require_department_admin(actor, department)
    supported_locales = {value for value, _ in DEPARTMENT_LOCALE_CHOICES}
    supported_timezones = {value for value, _ in DEPARTMENT_TIMEZONE_CHOICES}
    if locale not in supported_locales:
        raise ValueError("Unsupported Department locale.")
    if timezone_name not in supported_timezones:
        raise ValueError("Unsupported Department timezone.")
    department = Department.objects.select_for_update().get(pk=department.pk)
    old_values = {"locale": department.locale, "timezone": department.timezone}
    new_values = {"locale": locale, "timezone": timezone_name}
    if old_values == new_values:
        return department
    department.locale = locale
    department.timezone = timezone_name
    department.save(update_fields=("locale", "timezone"))
    record_event(
        action="authorization.department_locale_time_policy_changed",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
        metadata={
            "old_locale": old_values["locale"],
            "new_locale": new_values["locale"],
            "old_timezone": old_values["timezone"],
            "new_timezone": new_values["timezone"],
        },
    )
    return department


@transaction.atomic
def set_system_department_tablet_lease(
    *, actor, department: Department, tablet_lease_days: int
) -> Department:
    """Set an existing department lease from the System Administrator context."""
    require_system_admin(actor)
    if tablet_lease_days < 3 or tablet_lease_days > 365:
        raise ValueError("Tablet lease duration must be between 3 and 365 days.")
    department = Department.objects.select_for_update().get(pk=department.pk)
    old_value = department.tablet_lease_days
    if old_value == tablet_lease_days:
        return department
    department.tablet_lease_days = tablet_lease_days
    department.save(update_fields=("tablet_lease_days",))
    record_event(
        action="authorization.department_tablet_lease_changed",
        actor_user=actor,
        department=department,
        target_type="department",
        target_uuid=department.id,
        metadata={"old_days": old_value, "new_days": tablet_lease_days},
    )
    return department


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
    department = Department.objects.select_for_update().get(pk=department.pk)
    system_actor = is_system_admin(actor)
    if system_actor:
        if department.status != Department.Status.ACTIVE:
            raise PermissionDenied("Department must be operational before administrator bootstrap.")
        if effective_department_admin_memberships(department).exists():
            raise PermissionDenied("System administrators may only bootstrap orphaned departments.")
    elif department.id not in active_department_ids(actor):
        raise PermissionDenied("Department administrator scope is required.")
    had_prior_authority = DepartmentMembership.objects.filter(department=department).exists()
    token, raw_token = create_setup_token(actor=actor, email=email, display_name=display_name)
    DepartmentMembership.objects.create(user=token.user, department=department, created_by=actor)
    record_event(
        action=(
            "authorization.department_admin_orphan_recovered"
            if system_actor and had_prior_authority
            else "authorization.department_admin_bootstrapped"
            if system_actor
            else "authorization.department_admin_provisioned"
        ),
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
def _locked_membership(membership: DepartmentMembership) -> DepartmentMembership:
    return (
        DepartmentMembership.objects.select_for_update()
        .select_related("user", "department")
        .get(pk=membership.pk)
    )


def _locked_department(department_id) -> Department:
    return Department.objects.select_for_update().get(pk=department_id)


def _require_tenant_lifecycle_actor(actor, department: Department) -> None:
    """Lifecycle administration belongs to the tenant, never the SaaS operator."""
    if is_system_admin(actor):
        raise PermissionDenied(
            "System administrators may not manage tenant administrator lifecycle."
        )
    require_department_admin(actor, department)


def _prevent_last_effective_admin_removal(
    *, department: Department, membership: DepartmentMembership
) -> None:
    if membership.status != DepartmentMembership.Status.ACTIVE:
        return
    effective = effective_department_admin_memberships(department).select_for_update()
    if not effective.filter(pk=membership.pk).exists():
        return
    if effective.count() <= 1:
        raise ValueError(
            "An operational department must retain one effective Department Administrator."
        )


@transaction.atomic
def suspend_department_admin(*, actor, membership: DepartmentMembership) -> None:
    department = _locked_department(membership.department_id)
    membership = _locked_membership(membership)
    _require_tenant_lifecycle_actor(actor, department)
    if membership.status != DepartmentMembership.Status.ACTIVE:
        raise ValueError("Only an active Department Administrator can be suspended.")
    _prevent_last_effective_admin_removal(department=department, membership=membership)
    membership.status = DepartmentMembership.Status.SUSPENDED
    membership.suspended_at = timezone.now()
    membership.suspended_by = actor
    membership.save(update_fields=("status", "suspended_at", "suspended_by"))
    record_event(
        action="authorization.department_admin_suspended",
        actor_user=actor,
        department=department,
        target_type="department_membership",
        target_uuid=membership.id,
    )


@transaction.atomic
def reinstate_department_admin(*, actor, membership: DepartmentMembership) -> None:
    department = _locked_department(membership.department_id)
    membership = _locked_membership(membership)
    _require_tenant_lifecycle_actor(actor, department)
    if membership.status != DepartmentMembership.Status.SUSPENDED:
        raise ValueError("Only a suspended Department Administrator can be reinstated.")
    if (
        DepartmentMembership.objects.filter(
            user=membership.user,
            role=membership.role,
            status=DepartmentMembership.Status.ACTIVE,
        )
        .exclude(pk=membership.pk)
        .exists()
    ):
        raise ValueError("This account already actively administers a department.")
    membership.status = DepartmentMembership.Status.ACTIVE
    membership.save(update_fields=("status",))
    record_event(
        action="authorization.department_admin_reinstated",
        actor_user=actor,
        department=department,
        target_type="department_membership",
        target_uuid=membership.id,
    )


@transaction.atomic
def revoke_department_admin(*, actor, membership: DepartmentMembership) -> None:
    department = _locked_department(membership.department_id)
    membership = _locked_membership(membership)
    _require_tenant_lifecycle_actor(actor, department)
    if membership.status not in (
        DepartmentMembership.Status.ACTIVE,
        DepartmentMembership.Status.SUSPENDED,
    ):
        raise ValueError(
            "Only active or suspended Department Administrator authority can be revoked."
        )
    _prevent_last_effective_admin_removal(department=department, membership=membership)
    membership.status = DepartmentMembership.Status.REVOKED
    membership.revoked_at = timezone.now()
    membership.revoked_by = actor
    membership.save(update_fields=("status", "revoked_at", "revoked_by"))
    record_event(
        action="authorization.department_admin_revoked",
        actor_user=actor,
        department=membership.department,
        target_type="department_membership",
        target_uuid=membership.id,
    )


@transaction.atomic
def revoke_station_admin(*, actor, assignment: StationAdminAssignment) -> None:
    department = _locked_department(assignment.station.department_id)
    assignment = (
        StationAdminAssignment.objects.select_for_update()
        .select_related("station__department")
        .get(pk=assignment.pk)
    )
    _require_tenant_lifecycle_actor(actor, department)
    if assignment.status not in (
        StationAdminAssignment.Status.ACTIVE,
        StationAdminAssignment.Status.SUSPENDED,
    ):
        raise ValueError("Only active or suspended Station Administrator authority can be revoked.")
    assignment.status = StationAdminAssignment.Status.REVOKED
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = actor
    assignment.save(update_fields=("status", "revoked_at", "revoked_by"))
    record_event(
        action="authorization.station_admin_revoked",
        actor_user=actor,
        department=assignment.station.department,
        station=assignment.station,
        target_type="station_admin_assignment",
        target_uuid=assignment.id,
    )


@transaction.atomic
def suspend_station_admin(*, actor, assignment: StationAdminAssignment) -> None:
    department = _locked_department(assignment.station.department_id)
    assignment = (
        StationAdminAssignment.objects.select_for_update()
        .select_related("station__department")
        .get(pk=assignment.pk)
    )
    _require_tenant_lifecycle_actor(actor, department)
    if assignment.status != StationAdminAssignment.Status.ACTIVE:
        raise ValueError("Only an active Station Administrator can be suspended.")
    assignment.status = StationAdminAssignment.Status.SUSPENDED
    assignment.suspended_at = timezone.now()
    assignment.suspended_by = actor
    assignment.save(update_fields=("status", "suspended_at", "suspended_by"))
    record_event(
        action="authorization.station_admin_suspended",
        actor_user=actor,
        department=assignment.station.department,
        station=assignment.station,
        target_type="station_admin_assignment",
        target_uuid=assignment.id,
    )


@transaction.atomic
def reinstate_station_admin(*, actor, assignment: StationAdminAssignment) -> None:
    department = _locked_department(assignment.station.department_id)
    assignment = (
        StationAdminAssignment.objects.select_for_update()
        .select_related("station__department")
        .get(pk=assignment.pk)
    )
    _require_tenant_lifecycle_actor(actor, department)
    if assignment.status != StationAdminAssignment.Status.SUSPENDED:
        raise ValueError("Only a suspended Station Administrator can be reinstated.")
    if (
        StationAdminAssignment.objects.filter(
            user=assignment.user,
            station=assignment.station,
            status=StationAdminAssignment.Status.ACTIVE,
        )
        .exclude(pk=assignment.pk)
        .exists()
    ):
        raise ValueError("This account already has active authority for this station.")
    assignment.status = StationAdminAssignment.Status.ACTIVE
    assignment.save(update_fields=("status",))
    record_event(
        action="authorization.station_admin_reinstated",
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
        status=StationAdminAssignment.Status.ACTIVE,
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


@transaction.atomic
def permanently_remove_administrator(*, actor, user, department: Department) -> None:
    """Revoke tenant authority and anonymize the retained historical account."""
    department = Department.objects.select_for_update().get(pk=department.pk)
    _require_tenant_lifecycle_actor(actor, department)
    if is_system_admin(user):
        raise ValueError(
            "A System Administrator cannot be permanently removed from tenant administration."
        )
    memberships = list(
        DepartmentMembership.objects.select_for_update().filter(user=user, department=department)
    )
    assignments = list(
        StationAdminAssignment.objects.select_for_update().filter(
            user=user, station__department=department
        )
    )
    if not memberships and not assignments:
        raise ValueError("User has no administrator authority in this department.")
    if (
        DepartmentMembership.objects.filter(user=user, status=DepartmentMembership.Status.ACTIVE)
        .exclude(department=department)
        .exists()
        or StationAdminAssignment.objects.filter(
            user=user, status=StationAdminAssignment.Status.ACTIVE
        )
        .exclude(station__department=department)
        .exists()
    ):
        raise ValueError(
            "Administrator has authority outside this department and cannot be permanently "
            "removed here."
        )
    for membership in memberships:
        _prevent_last_effective_admin_removal(department=department, membership=membership)
        if membership.status != DepartmentMembership.Status.REVOKED:
            membership.status = DepartmentMembership.Status.REVOKED
            membership.revoked_at = timezone.now()
            membership.revoked_by = actor
            membership.save(update_fields=("status", "revoked_at", "revoked_by"))
    for assignment in assignments:
        if assignment.status != StationAdminAssignment.Status.REVOKED:
            assignment.status = StationAdminAssignment.Status.REVOKED
            assignment.revoked_at = timezone.now()
            assignment.revoked_by = actor
            assignment.save(update_fields=("status", "revoked_at", "revoked_by"))
    permanently_deactivate_and_anonymize_user(user=user)
    record_event(
        action="authorization.administrator_permanently_removed",
        actor_user=actor,
        department=department,
        target_type="user",
        target_uuid=user.id,
    )
