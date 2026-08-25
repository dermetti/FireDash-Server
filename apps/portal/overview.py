"""Small, scoped attention summaries for the authenticated administration shell."""

from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.authorization.scopes import orphaned_departments
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet
from apps.tablets.queries import operationally_unassigned_tablets


@dataclass(frozen=True)
class AttentionItem:
    level: str
    count: int
    text: str
    url: str


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
    publication_attention = (
        DatasetScopeState.objects.filter(department=department)
        .filter(
            Q(dirty_since__isnull=False) | Q(publications__status=DatasetPublication.Status.FAILED)
        )
        .distinct()
        .count()
    )
    if publication_attention:
        publication_text = (
            f"{publication_attention} publication "
            f"scope{'s' if publication_attention != 1 else ''} need attention"
        )
        items.append(
            AttentionItem(
                "warning",
                publication_attention,
                publication_text,
                reverse("publications-list", args=[department.id]),
            )
        )
    return items


def station_attention(station: Station) -> list[AttentionItem]:
    """Station-scoped subset; it never selects another station implicitly."""
    current_tablets = Tablet.objects.filter(
        vehicle_assignments__vehicle__station=station,
        vehicle_assignments__valid_until__isnull=True,
        vehicle_assignments__ended_at__isnull=True,
    ).distinct()
    items: list[AttentionItem] = []
    stale = AppInstallation.objects.filter(
        tablet__in=current_tablets, status=AppInstallation.Status.STALE
    ).count()
    lost = current_tablets.filter(status=Tablet.Status.LOST).count()
    if stale:
        items.append(
            AttentionItem(
                "warning",
                stale,
                f"{stale} stale tablet installation{'s' if stale != 1 else ''}",
                reverse("tablet-list", args=[station.department_id]),
            )
        )
    if lost:
        items.append(
            AttentionItem(
                "danger",
                lost,
                f"{lost} tablet{'s are' if lost != 1 else ' is'} marked lost",
                reverse("tablet-list", args=[station.department_id]),
            )
        )
    publication_attention = (
        DatasetScopeState.objects.filter(department=station.department, station=station)
        .filter(
            Q(dirty_since__isnull=False) | Q(publications__status=DatasetPublication.Status.FAILED)
        )
        .distinct()
        .count()
    )
    if publication_attention:
        publication_text = (
            f"{publication_attention} station publication "
            f"scope{'s' if publication_attention != 1 else ''} need attention"
        )
        items.append(
            AttentionItem(
                "warning",
                publication_attention,
                publication_text,
                reverse("publications-list", args=[station.department_id]),
            )
        )
    return items


def attention_for_request(
    request, *, department: Department | None = None, station: Station | None = None
):
    """Compute attention once per request even when both shell and page need it."""
    key = (
        f"_firedash_attention_{department.id if department else station.id if station else 'none'}"
    )
    if not hasattr(request, key):
        setattr(
            request,
            key,
            department_attention(department)
            if department is not None
            else station_attention(station)
            if station is not None
            else [],
        )
    return getattr(request, key)
