from apps.authorization.models import SystemRole
from apps.organizations.models import Department, Station


def is_system_admin(user) -> bool:
    return bool(
        user.is_authenticated and SystemRole.objects.filter(user=user, active=True).exists()
    )


def active_department_ids(user):
    if not user.is_authenticated:
        return Department.objects.none().values_list("id", flat=True)
    return Department.objects.filter(
        status=Department.Status.ACTIVE,
        memberships__user=user,
        memberships__active=True,
    ).values_list("id", flat=True)


def managed_department_ids(user):
    """Management scope ignores resource state; operational scope does not."""
    if not user.is_authenticated:
        return Department.objects.none().values_list("id", flat=True)
    return Department.objects.filter(
        memberships__user=user,
        memberships__active=True,
    ).values_list("id", flat=True)


def active_station_ids(user):
    if not user.is_authenticated:
        return Station.objects.none().values_list("id", flat=True)
    department_stations = Station.objects.filter(
        department_id__in=active_department_ids(user), active=True
    )
    assigned_stations = Station.objects.filter(
        admin_assignments__user=user,
        admin_assignments__active=True,
        active=True,
        department__status=Department.Status.ACTIVE,
    )
    return (department_stations | assigned_stations).values_list("id", flat=True).distinct()


def can_manage_department(user, department: Department) -> bool:
    return department.id in managed_department_ids(user)


def can_manage_station(user, station: Station) -> bool:
    return station.active and station.id in active_station_ids(user)
