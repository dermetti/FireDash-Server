"""Read-only tablet query composition for the management UI and future dashboards.

These helpers never mutate state; mutations live in ``apps.tablets.services`` and
``apps.assignments.services``.
"""

from django.db.models import Count, Prefetch

from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Vehicle
from apps.tablets.models import AppInstallation, Tablet


def tablet_status_counts(department: Department) -> dict[str, int]:
    """Return status counts for a department's tablets, keyed for the status summary."""
    rows = Tablet.objects.filter(department=department).values("status").annotate(total=Count("id"))
    counts = {row["status"]: row["total"] for row in rows}
    return {
        "total": sum(counts.values()),
        "active": counts.get(Tablet.Status.ACTIVE, 0),
        "pending": counts.get(Tablet.Status.PENDING, 0),
        "stale": counts.get(Tablet.Status.STALE, 0),
        "removed": counts.get(Tablet.Status.REMOVED, 0),
        "lost": counts.get(Tablet.Status.LOST, 0),
        "retired": counts.get(Tablet.Status.RETIRED, 0),
    }


def current_vehicle(tablet: Tablet) -> Vehicle | None:
    """Return the vehicle of the tablet's current open assignment, if any."""
    assignment = (
        tablet.vehicle_assignments.filter(valid_until__isnull=True, ended_at__isnull=True)
        .select_related("vehicle__station")
        .first()
    )
    return assignment.vehicle if assignment is not None else None


def tablet_adoption_ready(tablet: Tablet) -> bool:
    """Presentation-only eligibility hint for the management UI.

    Mirrors the read-only prerequisites of ``_require_operational_tablet`` (the
    mutating service remains authoritative). This helper is NOT an authorization or
    integrity boundary: the service re-validates every rule before issuing an
    invitation, so a stale/racy "ready" hint can never bypass service validation.
    """
    if tablet.department.status != Department.Status.ACTIVE or not tablet.active:
        return False
    if tablet.status in (Tablet.Status.REMOVED, Tablet.Status.LOST, Tablet.Status.RETIRED):
        return False
    return tablet.vehicle_assignments.filter(
        ended_at__isnull=True,
        valid_until__isnull=True,
        vehicle__active=True,
        vehicle__station__active=True,
    ).exists()


def tablets_with_current_state(queryset):
    """Attach the current installation and open vehicle assignment for list rendering."""
    return queryset.prefetch_related(
        Prefetch(
            "installations",
            queryset=AppInstallation.objects.filter(
                status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
            ),
            to_attr="current_installations",
        ),
        Prefetch(
            "vehicle_assignments",
            queryset=TabletVehicleAssignment.objects.filter(
                valid_until__isnull=True, ended_at__isnull=True
            ).select_related("vehicle__station"),
            to_attr="current_assignment",
        ),
    )
