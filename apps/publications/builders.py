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
from apps.publications.models import DatasetSourceRevision
from apps.publications.pdf_bundles import PdfBundleError, read_accepted_pdf
from apps.publications.registry import DatasetTypeDefinition
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan, PhonebookEntry

logger = logging.getLogger(__name__)

MAX_HYDRANT_STATUS_BUCKETS = 50
BUILDERS: dict[str, Callable[..., dict[str, object]]] = {}
ARTIFACT_BUILDERS: dict[str, Callable[..., bytes]] = {}
SOURCE_BUILDERS: dict[str, Callable[..., dict[str, object]]] = {}
VALIDATORS: dict[str, Callable[..., None]] = {}


class PublicationBuildError(ValueError):
    pass


def source_fingerprint(*, definition: DatasetTypeDefinition, department, station) -> str:
    """Return the stable hash of the logical content distributed for a scope.

    The representation intentionally excludes source revisions, build times,
    ciphertext, signatures and artifact metadata.  It is therefore safe to
    compare with a successful publication after a canonical change was later
    reverted.
    """
    return source_fingerprint_for_payload(
        build_source_payload(definition=definition, department=department, station=station)
    )


def source_fingerprint_for_payload(payload: dict[str, object]) -> str:
    if set(payload) == {"_exact_source_sha256"} and isinstance(
        payload["_exact_source_sha256"], str
    ):
        return payload["_exact_source_sha256"]
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def build_source_payload(
    *, definition: DatasetTypeDefinition, department, station
) -> dict[str, object]:
    """Build deterministic publishable source content without volatile fields."""
    try:
        builder = SOURCE_BUILDERS[definition.builder_service]
    except KeyError as error:
        raise PublicationBuildError("No source fingerprint builder is available.") from error
    return builder(department=department, station=station)


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


def _build_phonebook(*, department, station, source_revision: int) -> dict[str, object]:
    entries = _phonebook_source_payload(department=department, station=station)["entries"]
    return {"entry_count": len(entries), "source_revision": source_revision}


def build_artifact(
    *,
    definition: DatasetTypeDefinition,
    department,
    station,
    source_revision: int,
    source_snapshot=None,
) -> bytes:
    try:
        artifact = ARTIFACT_BUILDERS[definition.builder_service](
            department=department,
            station=station,
            source_revision=source_revision,
            source_snapshot=source_snapshot,
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


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    return info


def _artifact_hydrants(*, department, station, source_revision: int, source_snapshot=None) -> bytes:
    if station is not None:
        raise PublicationBuildError("Hydrant artifact requires a department scope.")
    payload = (
        source_snapshot
        if source_snapshot is not None
        else _hydrant_source_payload(department=department, station=station)
    )
    return _json_bytes(payload | {"source_revision": source_revision})


def _artifact_personnel(
    *, department, station, source_revision: int, source_snapshot=None
) -> bytes:
    if station is None or station.department_id != department.id:
        raise PublicationBuildError("Personnel artifact requires a station in the department.")
    payload = (
        source_snapshot
        if source_snapshot is not None
        else _personnel_source_payload(department=department, station=station)
    )
    return _json_bytes(payload | {"source_revision": source_revision})


def _artifact_fire_plans(
    *, department, station, source_revision: int, source_snapshot=None
) -> bytes:
    if station is not None:
        raise PublicationBuildError("Fire-plan artifact requires a department scope.")
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = (
            source_snapshot
            if source_snapshot is not None
            else _fire_plan_source_payload(department=department, station=station)
        ).get("fire_plans", [])
        for entry in manifest:
            try:
                plan = FirePlan.objects.get(pk=entry["id"], department=department)
            except FirePlan.DoesNotExist as error:
                raise PublicationBuildError(
                    "Accepted fire-plan document is no longer available."
                ) from error
            try:
                document = read_accepted_pdf(
                    document_key=plan.document_key,
                    accepted_root=settings.REFERENCE_DATA_ACCEPTED_ROOT,
                )
            except PdfBundleError as error:
                raise PublicationBuildError(
                    "Accepted fire-plan document is unavailable."
                ) from error
            if hashlib.sha256(document).hexdigest() != entry["sha256"]:
                raise PublicationBuildError(
                    "Accepted fire-plan document hash does not match metadata."
                )
            archive_name = f"plans/{plan.id}.pdf"
            archive.writestr(_zip_info(archive_name), document)
        archive.writestr(
            _zip_info("manifest.json"),
            _json_bytes({"source_revision": source_revision, "fire_plans": manifest}),
        )
    return output.getvalue()


BUILDERS.update(
    {
        "department_hydrants": _build_hydrants,
        "department_fire_plans": _build_fire_plans,
        "station_personnel": _build_personnel,
        "department_klgv_plans": _build_klgv_plans,
        "department_phonebook": _build_phonebook,
        "station_phonebook": _build_phonebook,
        "test_department_incidents": lambda *, department, station, source_revision: {
            "incident_count": 0,
            "source_revision": source_revision,
        },
    }
)
VALIDATORS["summary"] = validate_summary


def _hydrant_source_payload(*, department, station) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Hydrant source requires a department scope.")
    features = []
    for hydrant in Hydrant.objects.filter(department=department, status="ACTIVE").order_by("id"):
        features.append(
            {
                "type": "Feature",
                "id": str(hydrant.id),
                "geometry": {
                    "type": "Point",
                    "coordinates": [hydrant.geometry.x, hydrant.geometry.y],
                },
                "properties": {
                    "external_identifier": hydrant.external_identifier,
                    "street": hydrant.street or None,
                    "house_number": hydrant.house_number or None,
                    "location": hydrant.location or None,
                    "hydrant_type": hydrant.hydrant_type,
                    "diameter_mm": hydrant.diameter_mm,
                    "status": hydrant.status,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features, "schema_version": 1}


def _personnel_source_payload(*, department, station) -> dict[str, object]:
    if station is None or station.department_id != department.id:
        raise PublicationBuildError("Personnel source requires a station in the department.")
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
    return {
        "station_id": str(station.id),
        "people": [
            {
                "id": str(assignment.person_id),
                "display_name": assignment.person.display_name,
                "incident_commander_eligible": assignment.person.incident_commander_eligible,
                "commander_email": (
                    assignment.person.incident_commander_email
                    if assignment.person.email_verified_at
                    else None
                ),
            }
            for assignment in assignments
        ],
    }


def _fire_plan_source_manifest(*, department, station) -> list[dict[str, object]]:
    if station is not None:
        raise PublicationBuildError("Fire-plan source requires a department scope.")
    return [
        {
            "id": str(plan.id),
            "external_identifier": plan.external_identifier or None,
            "object_name": plan.object_name or None,
            "address": plan.address or None,
            "postal_code": plan.postal_code or None,
            "city": plan.city or None,
            "fsd_location": plan.fsd_location or None,
            "bmz_location": plan.bmz_location or None,
            "rwa_info": plan.rwa_info or None,
            "longitude": plan.location.x if plan.location is not None else None,
            "latitude": plan.location.y if plan.location is not None else None,
            "sha256": plan.sha256,
            "page_count": plan.page_count,
            "path": f"plans/{plan.id}.pdf",
        }
        for plan in FirePlan.objects.filter(department=department, active=True).order_by("id")
    ]


def _fire_plan_source_payload(*, department, station) -> dict[str, object]:
    return {"fire_plans": _fire_plan_source_manifest(department=department, station=station)}


def _klgv_source_manifest(*, department, station) -> list[dict[str, object]]:
    if station is not None:
        raise PublicationBuildError("KLGV plan source requires a department scope.")
    return [
        {
            "id": str(plan.id),
            "external_identifier": plan.external_identifier or None,
            "object_name": plan.object_name,
            "address": plan.address,
            "postal_code": plan.postal_code,
            "city": plan.city,
            "longitude": plan.location.x if plan.location is not None else None,
            "latitude": plan.location.y if plan.location is not None else None,
            "sha256": plan.sha256,
            "page_count": plan.page_count,
        }
        for plan in KlgvPlan.objects.filter(department=department, active=True).order_by("id")
    ]


def _klgv_source_payload(*, department, station) -> dict[str, object]:
    return {"klgv_plans": _klgv_source_manifest(department=department, station=station)}


def _phonebook_source_payload(*, department, station) -> dict[str, object]:
    if station is not None and station.department_id != department.id:
        raise PublicationBuildError("Phonebook source requires a station in the department.")
    entries = PhonebookEntry.objects.filter(department=department)
    entries = (
        entries.filter(station=station)
        if station is not None
        else entries.filter(station__isnull=True)
    )
    return {
        "entries": [
            {
                "id": str(entry.id),
                "first_name": entry.first_name or None,
                "last_name": entry.last_name or None,
                "organization_unit": entry.organization_unit or None,
                "function": entry.function or None,
                "phone_number": entry.phone_number,
            }
            for entry in entries.order_by("id")
        ]
    }


def _artifact_phonebook(
    *, department, station, source_revision: int, source_snapshot=None
) -> bytes:
    payload = source_snapshot or _phonebook_source_payload(department=department, station=station)
    return _json_bytes(payload | {"source_revision": source_revision})


def _dangerous_goods_source_payload(*, department, station) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Dangerous-goods source requires a department scope.")
    source = (
        DatasetSourceRevision.objects.filter(
            scope_state__department=department,
            scope_state__station__isnull=True,
            scope_state__dataset_type_code="dangerous_goods",
        )
        .order_by("-source_revision")
        .first()
    )
    if source is None:
        raise PublicationBuildError("Dangerous-goods source is unavailable.")
    return {"_exact_source_sha256": source.sha256}


def _dangerous_goods_source(
    *, department, source_revision: int, source_snapshot
) -> DatasetSourceRevision:
    source = DatasetSourceRevision.objects.filter(
        scope_state__department=department,
        scope_state__station__isnull=True,
        scope_state__dataset_type_code="dangerous_goods",
        source_revision=source_revision,
        sha256=source_snapshot.get("_exact_source_sha256")
        if isinstance(source_snapshot, dict)
        else None,
    ).first()
    if source is None:
        raise PublicationBuildError("Frozen dangerous-goods source is unavailable.")
    return source


def _build_dangerous_goods(*, department, station, source_revision: int) -> dict[str, object]:
    if station is not None:
        raise PublicationBuildError("Dangerous-goods builder requires a department scope.")
    source = DatasetSourceRevision.objects.filter(
        scope_state__department=department,
        scope_state__station__isnull=True,
        scope_state__dataset_type_code="dangerous_goods",
        source_revision=source_revision,
    ).first()
    if source is None:
        raise PublicationBuildError("Frozen dangerous-goods source is unavailable.")
    return {
        "goods_count": int(source.import_summary["goods_count"]),
        "eri_card_count": int(source.import_summary["eri_card_count"]),
        "source_revision": source_revision,
    }


def _artifact_dangerous_goods(
    *, department, station, source_revision: int, source_snapshot=None
) -> bytes:
    if station is not None:
        raise PublicationBuildError("Dangerous-goods artifact requires a department scope.")
    return bytes(
        _dangerous_goods_source(
            department=department, source_revision=source_revision, source_snapshot=source_snapshot
        ).plaintext
    )


SOURCE_BUILDERS.update(
    {
        "department_hydrants": _hydrant_source_payload,
        "department_fire_plans": _fire_plan_source_payload,
        "station_personnel": _personnel_source_payload,
        "department_klgv_plans": _klgv_source_payload,
        "department_phonebook": _phonebook_source_payload,
        "station_phonebook": _phonebook_source_payload,
        "test_department_incidents": lambda **_: {"incidents": []},
    }
)
ARTIFACT_BUILDERS.update(
    {
        "department_hydrants": _artifact_hydrants,
        "department_fire_plans": _artifact_fire_plans,
        "station_personnel": _artifact_personnel,
        "department_phonebook": _artifact_phonebook,
        "station_phonebook": _artifact_phonebook,
        "test_department_incidents": lambda **_: _json_bytes({"incidents": []}),
    }
)

# Defined after the ordinary JSON builders because this source is raw retained
# bytes rather than a canonical ORM projection.
BUILDERS["dangerous_goods"] = _build_dangerous_goods
SOURCE_BUILDERS["dangerous_goods"] = _dangerous_goods_source_payload
ARTIFACT_BUILDERS["dangerous_goods"] = _artifact_dangerous_goods


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
        "department_phonebook": ("entry_count",),
        "station_phonebook": ("entry_count",),
        "dangerous_goods": ("goods_count", "eri_card_count"),
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
