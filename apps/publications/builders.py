from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.publications.registry import DatasetTypeDefinition
from apps.reference_data.models import FirePlan, Hydrant

MAX_HYDRANT_STATUS_BUCKETS = 50


class PublicationBuildError(ValueError):
    pass


def build_summary(
    *, definition: DatasetTypeDefinition, department, station, source_revision: int
) -> dict[str, object]:
    builders: dict[str, Callable[..., dict[str, object]]] = {
        "department_hydrants": _build_hydrants,
        "department_fire_plans": _build_fire_plans,
        "station_personnel": _build_personnel,
    }
    try:
        summary = builders[definition.builder_service](
            department=department,
            station=station,
            source_revision=source_revision,
        )
    except KeyError as error:
        raise PublicationBuildError("No registered builder is available.") from error
    validate_summary(definition=definition, summary=summary)
    return summary


def validate_summary(*, definition: DatasetTypeDefinition, summary: Any) -> None:
    if not isinstance(summary, dict) or set(summary) != set(definition.summary_schema):
        raise PublicationBuildError("Builder returned an invalid summary schema.")
    for field, kind in definition.summary_schema.items():
        value = summary[field]
        if kind == "non_negative_integer" and (not isinstance(value, int) or value < 0):
            raise PublicationBuildError("Builder returned an invalid summary value.")
        if kind == "non_negative_integer" and value > settings.PUBLICATION_BUILD_SUMMARY_MAX_ITEMS:
            raise PublicationBuildError("Builder summary exceeds the configured item limit.")
        if kind == "uuid" and (not isinstance(value, str) or len(value) != 36):
            raise PublicationBuildError("Builder returned an invalid summary value.")
        if kind == "bounded_string_integer_map":
            if not isinstance(value, dict) or len(value) > MAX_HYDRANT_STATUS_BUCKETS:
                raise PublicationBuildError(
                    "Builder summary exceeds the configured category limit."
                )
            if any(
                not isinstance(key, str)
                or len(key) > 128
                or not isinstance(count, int)
                or count < 0
                for key, count in value.items()
            ):
                raise PublicationBuildError("Builder returned an invalid summary value.")


def _build_hydrants(*, department, station, source_revision: int) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Hydrant builder requires a department scope.")
    status_rows = list(
        Hydrant.objects.filter(department=department, active=True)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")[: MAX_HYDRANT_STATUS_BUCKETS + 1]
    )
    if len(status_rows) > MAX_HYDRANT_STATUS_BUCKETS:
        raise PublicationBuildError("Hydrant status categories exceed the configured limit.")
    return {
        "active_count": sum(row["count"] for row in status_rows),
        "source_revision": source_revision,
        "status_counts": {row["status"]: row["count"] for row in status_rows},
    }


def _build_fire_plans(*, department, station, source_revision: int) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Fire-plan builder requires a department scope.")
    aggregates = FirePlan.objects.filter(department=department, active=True).aggregate(
        active_document_count=Count("id"),
        total_accepted_bytes=Sum("file_size"),
        total_pages=Sum("page_count"),
    )
    return {
        "active_document_count": aggregates["active_document_count"],
        "total_accepted_bytes": aggregates["total_accepted_bytes"] or 0,
        "total_pages": aggregates["total_pages"] or 0,
        "source_revision": source_revision,
    }


def _build_personnel(*, department, station, source_revision: int) -> dict[str, object]:
    if station is None or station.department_id != department.id:
        raise PublicationBuildError("Personnel builder requires a station in the department.")
    # This mirrors the current personnel visibility predicate for active assignments.
    visible_people = PersonnelStationAssignment.objects.filter(
        station=station,
        person__department=department,
        person__active=True,
        ended_at__isnull=True,
        valid_from__lte=timezone.now(),
    )
    visible_people = visible_people.filter(
        Q(valid_until__isnull=True) | Q(valid_until__gt=timezone.now())
    )
    aggregates = visible_people.aggregate(
        person_count=Count("person_id", distinct=True),
        commander_eligible_count=Count(
            "person_id", filter=Q(person__incident_commander_eligible=True), distinct=True
        ),
        verified_commander_email_count=Count(
            "person_id", filter=Q(person__email_verified_at__isnull=False), distinct=True
        ),
    )
    return {
        "person_count": aggregates["person_count"],
        "station_id": str(station.id),
        "commander_eligible_count": aggregates["commander_eligible_count"],
        "verified_commander_email_count": aggregates["verified_commander_email_count"],
        "source_revision": source_revision,
    }
