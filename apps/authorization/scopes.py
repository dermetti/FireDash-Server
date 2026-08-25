from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.organizations.models import Department, Station


def is_system_admin(user) -> bool:
    return bool(
        user.is_authenticated
        and user.is_active
        and SystemRole.objects.filter(user=user, active=True).exists()
    )


def effective_department_admin_memberships(department: Department | None = None):
    """The single authoritative query for effective Department administration."""
    memberships = DepartmentMembership.objects.filter(
        status=DepartmentMembership.Status.ACTIVE,
        user__is_active=True,
        department__status=Department.Status.ACTIVE,
    )
    return memberships.filter(department=department) if department is not None else memberships


def orphaned_departments():
    """Operational Departments without an effective Department Administrator."""
    effective_department_ids = effective_department_admin_memberships().values("department_id")
    return Department.objects.filter(status=Department.Status.ACTIVE).exclude(
        id__in=effective_department_ids
    )


def active_department_ids(user):
    if not user.is_authenticated or not user.is_active:
        return Department.objects.none().values_list("id", flat=True)
    return Department.objects.filter(
        status=Department.Status.ACTIVE,
        memberships__user=user,
        memberships__status=DepartmentMembership.Status.ACTIVE,
        memberships__user__is_active=True,
    ).values_list("id", flat=True)


def managed_department_ids(user):
    """Management scope ignores resource state; operational scope does not."""
    if not user.is_authenticated or not user.is_active:
        return Department.objects.none().values_list("id", flat=True)
    return Department.objects.filter(
        memberships__user=user,
        memberships__status=DepartmentMembership.Status.ACTIVE,
        memberships__user__is_active=True,
    ).values_list("id", flat=True)


def active_station_ids(user):
    if not user.is_authenticated or not user.is_active:
        return Station.objects.none().values_list("id", flat=True)
    assigned_stations = Station.objects.filter(
        admin_assignments__user=user,
        admin_assignments__status=StationAdminAssignment.Status.ACTIVE,
        admin_assignments__user__is_active=True,
        active=True,
        department__status=Department.Status.ACTIVE,
    )
    return assigned_stations.values_list("id", flat=True).distinct()


def can_manage_department(user, department: Department) -> bool:
    return department.id in managed_department_ids(user)


def can_manage_station(user, station: Station) -> bool:
    return station.active and station.id in active_station_ids(user)


class StationAdminContextError(ValueError):
    """A station administrator has zero or multiple active station assignments."""


def station_admin_station(user) -> Station | None:
    """Resolve the single active station a station-only administrator manages.

    Returns ``None`` when the user has no active station assignments. Raises
    :class:`StationAdminContextError` when the user has more than one active
    station, which the product model treats as an inconsistent configuration
    that must fail safely rather than be silently resolved.
    """
    station_ids = list(active_station_ids(user))
    if not station_ids:
        return None
    if len(station_ids) > 1:
        raise StationAdminContextError(
            "Station administrator has multiple active station assignments."
        )
    return Station.objects.get(pk=station_ids[0])
