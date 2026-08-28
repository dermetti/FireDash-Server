"""Small, scoped attention summaries for the authenticated administration shell."""

from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from apps.authorization.scopes import orphaned_departments
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.publications.models import DatasetScopeState
from apps.publications.state import (
    FAILED,
    NEEDS_REBUILD,
    NOT_PUBLISHED,
    READY_TO_PUBLISH,
    scope_states_for_attention,
)
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet
from apps.tablets.queries import operationally_unassigned_tablets


@dataclass(frozen=True)
class AttentionItem:
    level: str
    count: int
    text: str
    url: str


@dataclass(frozen=True)
class OperationalMetric:
    text: str
    url: str


def _publication_attention(department: Department) -> list[AttentionItem]:
    """Return one actionable item per scope from the narrow publication read model."""
    scopes = (
        DatasetScopeState.objects.filter(department=department)
        .select_related("station")
        .only(
            "id",
            "dataset_type_code",
            "source_revision",
            "current_source_fingerprint",
            "dirty_since",
            "updated_at",
            "station_id",
            "latest_built_publication_id",
            "current_published_publication_id",
            "station__short_code",
        )
    )
    reasons = {
        NOT_PUBLISHED: "Not published",
        NEEDS_REBUILD: "Changes not published",
        FAILED: "Update failed",
        READY_TO_PUBLISH: "Ready to publish",
    }
    return [
        AttentionItem(
            "danger" if attention_state == FAILED else "warning",
            1,
            (
                f"{scope.dataset_type_code}"
                f"{' - ' + scope.station.short_code if scope.station_id else ''}: "
                f"{reasons[attention_state]}"
            ),
            reverse("publications-scope-detail", args=[scope.id]),
        )
        for scope, state in scope_states_for_attention(list(scopes))
        for attention_state in (
            NOT_PUBLISHED
            if state == NEEDS_REBUILD and scope.current_published_publication_id is None
            else state,
        )
        if attention_state in reasons
    ]


def system_attention() -> list[AttentionItem]:
    orphaned = orphaned_departments().order_by("name", "id")
    count = orphaned.count()
    if not count:
        return []
    first = orphaned.first()
    assert first is not None
    return [
        AttentionItem(
            "warning",
            count,
            (
                f"{count} operational department{'s' if count != 1 else ''} require "
                "administrator recovery"
            ),
            reverse("portal-system-department", args=[first.id]),
        )
    ]


def department_attention(department: Department) -> list[AttentionItem]:
    """Return only actionable, authoritative department signals."""
    items: list[AttentionItem] = []
    unassigned = operationally_unassigned_tablets(department).count()
    if unassigned:
        items.append(
            AttentionItem(
                "warning",
                unassigned,
                f"{unassigned} tablet{'s are' if unassigned != 1 else ' is'} unassigned",
                reverse("tablet-list", args=[department.id]) + "?assignment=unassigned",
            )
        )
    stale = AppInstallation.objects.filter(
        tablet__department=department, status=AppInstallation.Status.STALE
    ).count()
    if stale:
        items.append(
            AttentionItem(
                "warning",
                stale,
                f"{stale} stale tablet installation{'s' if stale != 1 else ''}",
                reverse("tablet-list", args=[department.id]),
            )
        )
    lost = Tablet.objects.filter(department=department, status=Tablet.Status.LOST).count()
    if lost:
        items.append(
            AttentionItem(
                "danger",
                lost,
                f"{lost} tablet{'s are' if lost != 1 else ' is'} marked lost",
                reverse("tablet-list", args=[department.id]),
            )
        )
    pending = AdoptionInvitation.objects.filter(
        tablet__department=department,
        used_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).count()
    if pending:
        items.append(
            AttentionItem(
                "info",
                pending,
                f"{pending} pending tablet adoption{'s' if pending != 1 else ''}",
                reverse("tablet-list", args=[department.id]),
            )
        )
    return items + _publication_attention(department)


def station_attention(station: Station) -> list[AttentionItem]:
    """Station-only users have no tablet or publication resolution route."""
    return []


def operational_summary(*, department: Department | None = None, station: Station | None = None):
    """Small, read-only operational context with destinations the role can use."""
    if department is not None:
        operational_tablets = Tablet.objects.filter(
            department=department, status__in=(Tablet.Status.ACTIVE, Tablet.Status.INACTIVE)
        ).count()
        return [
            OperationalMetric(
                f"{department.stations.filter(active=True).count()} active station(s)",
                reverse("portal-stations", args=[department.id]),
            ),
            OperationalMetric(
                f"{operational_tablets} operational tablet(s)",
                reverse("tablet-list", args=[department.id]),
            ),
            OperationalMetric(
                f"{DatasetScopeState.objects.filter(department=department).count()} "
                "publication scope(s)",
                reverse("publications-list", args=[department.id]),
            ),
        ]
    if station is not None:
        people = Person.objects.filter(
            active=True, station_assignments__station=station
        ).distinct().count()
        return [
            OperationalMetric(
                f"{people} active station personnel",
                reverse("personnel-list", args=[station.department_id]),
            )
        ]
    active_departments = Department.objects.filter(status=Department.Status.ACTIVE).count()
    return [
        OperationalMetric(
            f"{active_departments} active department(s)", reverse("portal-system-departments")
        ),
        OperationalMetric("API compatibility policies", reverse("portal-system-api-compatibility")),
    ]


def attention_for_request(
    request, *, department: Department | None = None, station: Station | None = None
):
    """Compute attention once per request even when both shell and page need it."""
    scope_id = department.id if department else station.id if station else "system"
    key = f"_firedash_attention_{scope_id}"
    if not hasattr(request, key):
        setattr(
            request,
            key,
            department_attention(department)
            if department is not None
            else station_attention(station)
            if station is not None
            else system_attention(),
        )
    return getattr(request, key)


def attention_total(attention: list[AttentionItem]) -> int:
    """The shell badge counts underlying actionable problems, not categories."""
    return sum(item.count for item in attention)
