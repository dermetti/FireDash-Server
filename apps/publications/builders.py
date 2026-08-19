import hashlib
import json
import logging
import zipfile
from collections.abc import Callable
from io import BytesIO
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.publications.pdf_bundles import (
    AcceptedPdfBundleDocument,
    PdfBundleError,
    build_pdf_bundle_v1,
    read_accepted_pdf,
)
from apps.publications.registry import DatasetTypeDefinition
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan

logger = logging.getLogger(__name__)

MAX_HYDRANT_STATUS_BUCKETS = 50
BUILDERS: dict[str, Callable[..., dict[str, object]]] = {}
ARTIFACT_BUILDERS: dict[str, Callable[..., bytes]] = {}
VALIDATORS: dict[str, Callable[..., None]] = {}


class PublicationBuildError(ValueError):
    pass


def build_summary(
    *, definition: DatasetTypeDefinition, department, station, source_revision: int
) -> dict[str, object]:
    try:
        summary = BUILDERS[definition.builder_service](
            department=department,
            station=station,
            source_revision=source_revision,
        )
    except KeyError as error:
        raise PublicationBuildError("No registered builder is available.") from error
    try:
        VALIDATORS[definition.validator_service](definition=definition, summary=summary)
    except KeyError as error:
        raise PublicationBuildError("No registered validator is available.") from error
    return summary


def validate_summary(*, definition: DatasetTypeDefinition, summary: Any) -> None:
    if not isinstance(summary, dict) or set(summary) != set(definition.summary_schema):
        raise PublicationBuildError("Builder returned an invalid summary schema.")
    for field, kind in definition.summary_schema.items():
        value = summary[field]
        if kind in {"item_count", "non_negative_integer"} and (type(value) is not int or value < 0):
            raise PublicationBuildError("Builder returned an invalid summary value.")
        if kind == "item_count" and value > settings.PUBLICATION_BUILD_SUMMARY_MAX_ITEMS:
            raise PublicationBuildError("Builder summary exceeds the configured item limit.")
        if kind == "uuid" and (not isinstance(value, str) or len(value) != 36):
            raise PublicationBuildError("Builder returned an invalid summary value.")
        if kind == "bounded_string_integer_map":
            if not isinstance(value, dict) or len(value) > MAX_HYDRANT_STATUS_BUCKETS:
                raise PublicationBuildError(
                    "Builder summary exceeds the configured category limit."
                )
            if any(
                not isinstance(key, str) or len(key) > 128 or type(count) is not int or count < 0
                for key, count in value.items()
            ):
                raise PublicationBuildError("Builder returned an invalid summary value.")
            if any(
                count > settings.PUBLICATION_BUILD_SUMMARY_MAX_ITEMS for count in value.values()
            ):
                raise PublicationBuildError("Builder summary exceeds the configured item limit.")


def _build_hydrants(*, department, station, source_revision: int) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Hydrant builder requires a department scope.")
    status_rows = list(
        Hydrant.objects.filter(department=department, status="ACTIVE")
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


def _build_klgv_plans(*, department, station, source_revision: int) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("KLGV plan builder requires a department scope.")
    aggregates = KlgvPlan.objects.filter(department=department, active=True).aggregate(
        document_count=Count("id"),
        total_accepted_bytes=Sum("file_size"),
        total_pages=Sum("page_count"),
    )
    return {
        "document_count": aggregates["document_count"],
        "total_accepted_bytes": aggregates["total_accepted_bytes"] or 0,
        "total_pages": aggregates["total_pages"] or 0,
        "source_revision": source_revision,
    }


def _artifact_klgv_plans(*, department, station, source_revision: int) -> bytes:
    if station is not None:
        raise PublicationBuildError("KLGV plan artifact requires a department scope.")
    documents = [
        AcceptedPdfBundleDocument(
            id=plan.id,
            title=plan.title,
            document_key=plan.document_key,
            sha256=plan.sanitized_pdf_sha256,
            page_count=plan.page_count,
            category=plan.category or None,
        )
        for plan in KlgvPlan.objects.filter(department=department, active=True).order_by("id")
    ]
    try:
        return build_pdf_bundle_v1(documents=documents, source_revision=source_revision)
    except PdfBundleError as error:
        raise PublicationBuildError("Accepted KLGV document is unavailable.") from error


def build_artifact(
    *, definition: DatasetTypeDefinition, department, station, source_revision: int
) -> bytes:
    try:
        artifact = ARTIFACT_BUILDERS[definition.builder_service](
            department=department, station=station, source_revision=source_revision
        )
    except KeyError as error:
        raise PublicationBuildError("No registered artifact builder is available.") from error
    if not isinstance(artifact, bytes):
        raise PublicationBuildError("Artifact builder returned invalid content.")
    if len(artifact) > settings.PUBLICATION_ARTIFACT_MAX_BYTES:
        logger.warning(
            "Publication artifact exceeds ceiling dataset_type_code=%s plaintext_bytes=%d "
            "ceiling_bytes=%d",
            definition.code,
            len(artifact),
            settings.PUBLICATION_ARTIFACT_MAX_BYTES,
        )
        raise PublicationBuildError(
            f"Publication artifact is {_mib(len(artifact))}; configured maximum is "
            f"{_mib(settings.PUBLICATION_ARTIFACT_MAX_BYTES)}."
        )
    return artifact


def _mib(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MiB"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _artifact_hydrants(*, department, station, source_revision: int) -> bytes:
    if station is not None:
        raise PublicationBuildError("Hydrant artifact requires a department scope.")
    features = []
    for hydrant in Hydrant.objects.filter(department=department, status="ACTIVE").order_by("id"):
        features.append(
            {
                "type": "Feature",
                "id": str(hydrant.id),
                "geometry": {
                    "type": "Point",
                    "coordinates": [hydrant.location.x, hydrant.location.y],
                },
                "properties": {
                    "external_identifier": hydrant.external_identifier,
                    "hydrant_type": hydrant.hydrant_type,
                    "diameter_mm": hydrant.diameter_mm,
                    "status": hydrant.status,
                },
            }
        )
    return _json_bytes(
        {
            "type": "FeatureCollection",
            "features": features,
            "schema_version": 1,
            "source_revision": source_revision,
        }
    )


def _artifact_personnel(*, department, station, source_revision: int) -> bytes:
    if station is None or station.department_id != department.id:
        raise PublicationBuildError("Personnel artifact requires a station in the department.")
    now = timezone.now()
    assignments = (
        PersonnelStationAssignment.objects.filter(
            station=station,
            person__department=department,
            person__active=True,
            ended_at__isnull=True,
            valid_from__lte=now,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .select_related("person")
        .order_by("person_id")
    )
    people = [
        {
            "id": str(a.person_id),
            "display_name": a.person.display_name,
            "incident_commander_eligible": a.person.incident_commander_eligible,
            "commander_email": a.person.incident_commander_email
            if a.person.email_verified_at
            else None,
        }
        for a in assignments
    ]
    return _json_bytes(
        {"station_id": str(station.id), "source_revision": source_revision, "people": people}
    )


def _artifact_fire_plans(*, department, station, source_revision: int) -> bytes:
    if station is not None:
        raise PublicationBuildError("Fire-plan artifact requires a department scope.")
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = []
        for plan in FirePlan.objects.filter(department=department, active=True).order_by("id"):
            try:
                document = read_accepted_pdf(
                    document_key=plan.document_key,
                    accepted_root=settings.REFERENCE_DATA_ACCEPTED_ROOT,
                )
            except PdfBundleError as error:
                raise PublicationBuildError(
                    "Accepted fire-plan document is unavailable."
                ) from error
            if hashlib.sha256(document).hexdigest() != plan.sha256:
                raise PublicationBuildError(
                    "Accepted fire-plan document hash does not match metadata."
                )
            archive_name = f"plans/{plan.id}.pdf"
            archive.writestr(archive_name, document)
            manifest.append(
                {
                    "id": str(plan.id),
                    "sha256": plan.sha256,
                    "page_count": plan.page_count,
                    "path": archive_name,
                }
            )
        archive.writestr(
            "manifest.json",
            _json_bytes({"source_revision": source_revision, "fire_plans": manifest}),
        )
    return output.getvalue()


BUILDERS.update(
    {
        "department_hydrants": _build_hydrants,
        "department_fire_plans": _build_fire_plans,
        "station_personnel": _build_personnel,
        "department_klgv_plans": _build_klgv_plans,
        "test_department_incidents": lambda *, department, station, source_revision: {
            "incident_count": 0,
            "source_revision": source_revision,
        },
    }
)
VALIDATORS["summary"] = validate_summary
ARTIFACT_BUILDERS.update(
    {
        "department_hydrants": _artifact_hydrants,
        "department_fire_plans": _artifact_fire_plans,
        "station_personnel": _artifact_personnel,
        "department_klgv_plans": _artifact_klgv_plans,
        "test_department_incidents": lambda **_: _json_bytes({"incidents": []}),
    }
)


def validate_built_summary(*, definition: DatasetTypeDefinition, summary: Any) -> None:
    try:
        VALIDATORS[definition.validator_service](definition=definition, summary=summary)
    except KeyError as error:
        raise PublicationBuildError("No registered validator is available.") from error


def build_change_summary(
    *, definition: DatasetTypeDefinition, previous: object, current: dict[str, object]
) -> dict[str, int]:
    previous_summary = previous if isinstance(previous, dict) else {}
    fields_by_type = {
        "department_hydrants": ("active_count", "diameter_mm_summary"),
        "department_fire_plans": ("active_document_count", "total_accepted_bytes", "total_pages"),
        "station_personnel": (
            "person_count",
            "commander_eligible_count",
            "verified_commander_email_count",
        ),
        "department_klgv_plans": ("document_count", "total_accepted_bytes", "total_pages"),
    }
    fields = fields_by_type.get(definition.code, ("item_count",))
    source_revision = current.get("source_revision")
    if not isinstance(source_revision, int):
        raise PublicationBuildError("Builder returned an invalid source revision.")
    changes: dict[str, int] = {"source_revision": source_revision}
    for field in fields:
        before = previous_summary.get(field, 0)
        after = current.get(field, 0)
        if isinstance(before, int) and isinstance(after, int):
            changes[f"{field}_delta"] = after - before
    return changes
