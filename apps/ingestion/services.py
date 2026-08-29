"""Canonical ingestion orchestration.

No function in this module calls a publication builder.  It commits canonical
rows first, then marks each unique publication scope dirty once in the same
transaction.  The worker pipeline remains the only artifact producer.
"""

import csv
import hashlib
import io
import json
import logging
import os
import shutil
import uuid
import zipfile
from typing import cast

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.assignments.services import transfer_home
from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.ingestion.models import ImportBatch
from apps.ingestion.parsers import (
    ImportValidationError,
    parse_hydrants,
    parse_personnel,
    parse_phonebook,
    parse_station_vehicles,
)
from apps.ingestion.pdf_packages import manifest_member_name, parse_pdf_package
from apps.ingestion.storage import ImportStorageError, read_staged, remove_staged, stage_upload
from apps.organizations.models import Station, Vehicle
from apps.organizations.services import create_station, create_vehicle
from apps.personnel.models import Person
from apps.personnel.services import create_person, set_commander_eligibility, update_person
from apps.publications.services import mark_dirty
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan, PhonebookEntry
from apps.reference_data.pdf_sandbox import PdfSanitizerContentError, PdfSanitizerError, sanitize
from apps.reference_data.pdf_validation import PdfValidationError, validate_pdf
from apps.reference_data.phonebook import (
    entry_fingerprint,
    find_entry_duplicate_candidates,
    normalize_phone_number,
)
from apps.reference_data.services import create_phonebook_entry, update_phonebook_entry
from apps.reference_data.storage import (
    StorageError as ReferenceDataStorageError,
)
from apps.reference_data.storage import (
    cleanup,
    output_path,
    promote_to_accepted,
    write_quarantine,
)


class ImportError(ValueError):
    """Safe user-facing ingestion failure."""


# Internal bulk-write chunk size. Large authoritative hydrant snapshots are
# written with bulk_create/bulk_update in batches of this size inside a single
# surrounding transaction; the chunk size never changes the logical outcome.
_INGESTION_BULK_BATCH_SIZE = 1_000

logger = logging.getLogger(__name__)


def _sanitize_log_filename(filename: str) -> str:
    """Return a log-safe, truncated ZIP member filename (no control characters)."""
    safe = "".join(
        ch if (ch.isprintable() and ch not in "\r\n\t") else "?" for ch in (filename or "")
    )
    return safe[:200]


def _log_sanitizer_failure(
    *,
    batch,
    domain: str,
    filename: str,
    source_sha256: str,
    job_uuid: str,
    stage: str,
    error: BaseException,
    input_bytes: int,
) -> None:
    """Log only safe diagnostic metadata for a rejected PDF sanitizer stage."""
    logger.warning(
        "PDF sanitizer rejected member batch_id=%s domain=%s filename=%r source_sha256=%s "
        "sanitizer_job=%s stage=%s exception=%s code=%s input_bytes=%d",
        str(batch.id),
        domain,
        _sanitize_log_filename(filename),
        source_sha256,
        job_uuid,
        stage,
        type(error).__name__,
        str(getattr(error, "code", "") or ""),
        input_bytes,
    )


def create_single_preview(
    *,
    actor,
    department,
    domain: str,
    values: dict[str, object],
    pdf_bytes: bytes | None = None,
    original_filename: str = "manual-entry",
    station=None,
) -> ImportBatch:
    """Normalize one form submission through the exact batch parser/apply path.

    UI callers never save a canonical record directly. A one-record CSV or a
    one-document ZIP is only a transport envelope; validation, diffing, stale
    checks, audit, and apply remain identical to file imports.
    """
    if domain == ImportBatch.Domain.HYDRANTS:
        payload = _csv_payload(
            (
                "external_identifier",
                "longitude",
                "latitude",
                "street",
                "house_number",
                "location",
                "hydrant_type",
                "diameter_mm",
                "status",
            ),
            values,
        )
        return create_preview(
            actor=actor,
            department=department,
            domain=domain,
            import_format=ImportBatch.Format.CSV,
            import_mode=ImportBatch.Mode.MERGE,
            filename=original_filename,
            payload=payload,
            station=station,
        )
    if domain == ImportBatch.Domain.PERSONNEL:
        payload = _csv_payload(
            (
                "personnel_number",
                "first_name",
                "last_name",
                "home_station",
                "incident_commander_eligible",
            ),
            values
            | {
                "home_station": values.get("home_station")
                or (station.short_code if station else "")
            },
        )
        return create_preview(
            actor=actor,
            department=department,
            domain=domain,
            import_format=ImportBatch.Format.CSV,
            import_mode=ImportBatch.Mode.UPSERT,
            filename=original_filename,
            payload=payload,
            station=station,
        )
    if domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        if not pdf_bytes:
            raise ImportError("A PDF document is required.")
        payload = _single_pdf_package(domain=domain, values=values, pdf_bytes=pdf_bytes)
        return create_preview(
            actor=actor,
            department=department,
            domain=domain,
            import_format=ImportBatch.Format.ZIP,
            import_mode=ImportBatch.Mode.UPSERT,
            filename=original_filename,
            payload=payload,
            station=station,
        )
    raise ImportError("Unsupported single import domain.")


def _csv_payload(fields: tuple[str, ...], values: dict[str, object]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerow(
        {
            field: (
                str(values[field]).lower()
                if isinstance(values.get(field), bool)
                else values.get(field, "")
            )
            for field in fields
        }
    )
    return output.getvalue().encode("utf-8")


def _single_pdf_package(*, domain: str, values: dict[str, object], pdf_bytes: bytes) -> bytes:
    fields: tuple[str, ...]
    if domain == ImportBatch.Domain.FIRE_PLANS:
        fields = (
            "external_identifier",
            "filename",
            "object_name",
            "address",
            "postal_code",
            "city",
            "longitude",
            "latitude",
            "fsd_location",
            "bmz_location",
            "rwa_info",
            "action",
        )
        row = {
            "external_identifier": values.get("external_identifier", ""),
            "filename": "document.pdf",
            "object_name": values.get("object_name", ""),
            "address": values.get("address", ""),
            "postal_code": values.get("postal_code", ""),
            "city": values.get("city", ""),
            "longitude": values.get("longitude", ""),
            "latitude": values.get("latitude", ""),
            "fsd_location": values.get("fsd_location", ""),
            "bmz_location": values.get("bmz_location", ""),
            "rwa_info": values.get("rwa_info", ""),
            "action": "upsert",
        }
    else:
        fields = (
            "external_identifier",
            "filename",
            "object_name",
            "address",
            "postal_code",
            "city",
            "longitude",
            "latitude",
            "action",
        )
        row = {
            "external_identifier": values.get("external_identifier", ""),
            "filename": "document.pdf",
            "object_name": values.get("object_name", ""),
            "address": values.get("address", ""),
            "postal_code": values.get("postal_code", ""),
            "city": values.get("city", ""),
            "longitude": values.get("longitude", ""),
            "latitude": values.get("latitude", ""),
            "action": "upsert",
        }
    manifest = _csv_payload(fields, row)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(manifest_member_name(domain), manifest)
        archive.writestr("document.pdf", pdf_bytes)
    return output.getvalue()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _hydrant_identifiers(rows) -> list[str]:
    """Return the deduplicated, sorted, non-empty identifiers present in *rows*.

    The baseline and comparison set are scoped to these identifiers so a manual
    one-record edit never materializes or fingerprints the whole department.
    """
    return sorted(
        {str(row["external_identifier"]) for row in rows if str(row["external_identifier"])}
    )


def _hydrant_baseline(*, department, identifiers) -> dict[str, str]:
    """Fingerprint only the canonical hydrants relevant to *identifiers*.

    The set of identifiers is taken directly from the import, so this scales
    O(number of imported identifiers), not O(department hydrant count).  All
    fields later read by ``_hydrant_business_values`` are declared in ``only``
    so no deferred-field N+1 query can occur.
    """
    identifiers = [identifier for identifier in identifiers if identifier]
    if not identifiers:
        return {}
    return {
        hydrant.external_identifier: _fingerprint(
            {
                "updated_at": hydrant.updated_at.isoformat(),
                "business_values": _hydrant_business_values(hydrant),
            }
        )
        for hydrant in Hydrant.objects.filter(
            department=department, external_identifier__in=identifiers
        ).only(
            "external_identifier",
            "updated_at",
            "status",
            "geometry",
            "street",
            "house_number",
            "location",
            "hydrant_type",
            "diameter_mm",
        )
    }


def _hydrant_business_values(hydrant_or_row) -> dict[str, object]:
    """The import's canonical business representation, never persistence metadata."""
    if isinstance(hydrant_or_row, Hydrant):
        return {
            "longitude": hydrant_or_row.geometry.x,
            "latitude": hydrant_or_row.geometry.y,
            "street": hydrant_or_row.street,
            "house_number": hydrant_or_row.house_number,
            "location": hydrant_or_row.location,
            "hydrant_type": hydrant_or_row.hydrant_type,
            "diameter_mm": hydrant_or_row.diameter_mm,
            "status": hydrant_or_row.status,
        }
    return {
        field: hydrant_or_row[field]
        for field in (
            "longitude",
            "latitude",
            "street",
            "house_number",
            "location",
            "hydrant_type",
            "diameter_mm",
            "status",
        )
    }


def _hydrant_changed_fields(*, current: Hydrant, proposed: dict[str, object]) -> list[str]:
    current_values = _hydrant_business_values(current)
    proposed_values = _hydrant_business_values(proposed)
    return [field for field in proposed_values if current_values[field] != proposed_values[field]]


def _personnel_baseline(*, department) -> dict[str, str]:
    homes = {
        assignment.person_id: str(assignment.station_id)
        for assignment in PersonnelStationAssignment.objects.filter(
            person__department=department,
            person__lifecycle_status=Person.LifecycleStatus.ACTIVE,
            assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
            ended_at__isnull=True,
            valid_until__isnull=True,
        ).only("person_id", "station_id")
    }
    return {
        number: _fingerprint(
            {
                "updated_at": person.updated_at.isoformat(),
                "active": person.active,
                "home_station_id": homes.get(person.id),
            }
        )
        for person in Person.objects.filter(
            department=department,
            personnel_number__isnull=False,
            lifecycle_status=Person.LifecycleStatus.ACTIVE,
        ).only("personnel_number", "updated_at", "active")
        for number in [person.personnel_number or ""]
    }


def _personnel_home_station_matches(*, department, reference: str) -> list[Station]:
    """Use the shared Station resolver: code first, then exact normalized name."""
    return _station_matches(department=department, short_code=reference, name=reference)


def _personnel_import_intent(*, rows, department, fallback_station=None):
    existing = {
        person.personnel_number: person
        for person in Person.objects.filter(
            department=department,
            personnel_number__isnull=False,
            lifecycle_status=Person.LifecycleStatus.ACTIVE,
        )
    }
    intent: list[dict[str, object]] = []
    review_items: list[dict[str, object]] = []
    for index, source in enumerate(rows):
        row = dict(source)
        key = f"personnel:{index}"
        row["key"] = key
        current = existing.get(row["personnel_number"])
        reference = str(row.get("home_station", "")).strip()
        if not reference and current is None and fallback_station is not None:
            row["home_station_resolution"] = {
                "state": "existing",
                "station_id": str(fallback_station.id),
            }
        elif not reference and current is not None:
            row["home_station_resolution"] = {"state": "retain"}
        elif not reference:
            row["home_station_resolution"] = {"state": "missing"}
            review_items.append(
                {
                    "key": key,
                    "kind": "personnel_missing_home_station",
                    "personnel_number": row["personnel_number"],
                    "display_name": f"{row['first_name']} {row['last_name']}",
                    "reference": "(blank)",
                }
            )
        else:
            matches = _personnel_home_station_matches(department=department, reference=reference)
            if len(matches) == 1:
                row["home_station_resolution"] = {
                    "state": "existing",
                    "station_id": str(matches[0].id),
                }
            elif len(matches) > 1:
                row["home_station_resolution"] = {"state": "ambiguous"}
                review_items.append(
                    {
                        "key": key,
                        "kind": "personnel_ambiguous_home_station",
                        "personnel_number": row["personnel_number"],
                        "display_name": f"{row['first_name']} {row['last_name']}",
                        "reference": reference,
                        "candidate_ids": [str(station.id) for station in matches],
                    }
                )
            else:
                row["home_station_resolution"] = {"state": "missing"}
                review_items.append(
                    {
                        "key": key,
                        "kind": "personnel_missing_home_station",
                        "personnel_number": row["personnel_number"],
                        "display_name": f"{row['first_name']} {row['last_name']}",
                        "reference": reference,
                    }
                )
        row["action"] = "new" if current is None else "existing"
        intent.append(row)
    return intent, review_items


def _station_reference_key(value: object) -> str:
    """Normalize an imported Station reference without changing its display value."""
    return " ".join(str(value or "").split()).casefold()


def _station_vehicle_baseline(*, department) -> dict[str, str]:
    """Fingerprint the canonical rows which Station/Vehicle import may observe."""
    stations = Station.objects.filter(department=department).only("id", "updated_at", "active")
    vehicles = Vehicle.objects.filter(department=department).only("id", "updated_at", "active")
    return {
        **{
            f"station:{station.id}": _fingerprint(
                {"updated_at": station.updated_at.isoformat(), "active": station.active}
            )
            for station in stations
        },
        **{
            f"vehicle:{vehicle.id}": _fingerprint(
                {"updated_at": vehicle.updated_at.isoformat(), "active": vehicle.active}
            )
            for vehicle in vehicles
        },
    }


def _station_matches(*, department, short_code: str, name: str) -> list[Station]:
    """Return same-department stations matching the preferred code or full name."""
    stations = list(
        Station.objects.filter(department=department, active=True).only(
            "id", "short_code", "name", "street", "house_number", "postal_code", "city", "active"
        )
    )
    code_key = _station_reference_key(short_code)
    name_key = _station_reference_key(name)
    if code_key:
        code_matches = [
            station
            for station in stations
            if _station_reference_key(station.short_code) == code_key
        ]
        if code_matches:
            return code_matches
    return [station for station in stations if _station_reference_key(station.name) == name_key]


def _phonebook_baseline(*, department) -> dict[str, str]:
    return {
        str(entry.id): entry_fingerprint(entry)
        for entry in PhonebookEntry.objects.filter(department=department).only(
            "id",
            "department_id",
            "station_id",
            "first_name",
            "last_name",
            "organization_unit",
            "function",
            "phone_number",
        )
    }


def _phonebook_intent(*, rows, department):
    intent, review_items = [], []
    for index, source in enumerate(rows):
        row = dict(source)
        scope = row.pop("scope")
        if _station_reference_key(scope) == "department":
            station = None
        else:
            matches = _station_matches(department=department, short_code=scope, name=scope)
            if len(matches) != 1:
                reason = "ambiguous" if matches else "unknown"
                raise ImportError(f"Row {index + 2}: {reason} Phonebook scope.")
            station = matches[0]
        row["phone_number"] = normalize_phone_number(str(row["phone_number"]))
        row["station_id"] = str(station.id) if station else None
        row["scope_label"] = station.name if station else "Department"
        row["row_index"] = index
        staged = PhonebookEntry(
            department=department,
            station=station,
            **{
                field: row[field]
                for field in (
                    "first_name",
                    "last_name",
                    "organization_unit",
                    "function",
                    "phone_number",
                )
            },
        )
        candidates = find_entry_duplicate_candidates(entry=staged, department=department)
        row["candidates"] = [
            {
                "id": str(candidate.second.id),
                "fingerprint": candidate.second_fingerprint,
                "reasons": list(candidate.reasons),
                "conflicts": list(candidate.conflicts),
                "name": candidate.second.display_name,
                "function": candidate.second.function,
                "phone_number": candidate.second.phone_number,
                "scope": candidate.second.scope_label,
            }
            for candidate in candidates
        ]
        row["key"] = f"phonebook:{index}"
        row["candidate_index"] = 0
        row["resolution"] = "create" if not candidates else "pending"
        intent.append(row)
        if candidates:
            review_items.append({"key": row["key"], "row_index": index})
    return intent, review_items


def phonebook_review_context(batch: ImportBatch) -> dict[str, object]:
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    current = next((row for row in rows if row.get("resolution") == "pending"), None)
    review_rows = [row for row in rows if row.get("candidates")]
    summary = {
        "create": sum(row.get("resolution") == "create" for row in rows),
        "update": sum(row.get("resolution") == "update" for row in rows),
        "skip": sum(row.get("resolution") == "skip" for row in rows),
        "errors": len(batch.validation_errors),
        "pending": sum(row.get("resolution") == "pending" for row in rows),
    }
    if current is not None:
        position = int(current.get("candidate_index", 0))
        candidates = current.get("candidates", [])
        current["candidate"] = candidates[position] if position < len(candidates) else None
        current["has_next"] = position + 1 < len(candidates)
    progress = None
    if current is not None:
        progress = {
            "current": next(
                index
                for index, row in enumerate(review_rows, start=1)
                if row.get("row_index") == current.get("row_index")
            ),
            "total": len(review_rows),
        }
    return {"current": current, "summary": summary, "progress": progress}


@transaction.atomic
def set_phonebook_reconciliation(*, actor, batch_id, row_index: int, action: str) -> ImportBatch:
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if (
        batch.status != ImportBatch.Status.PREVIEW_READY
        or batch.domain != ImportBatch.Domain.PHONEBOOK
    ):
        raise ImportError("Phonebook review is unavailable.")
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    if row_index < 0 or row_index >= len(rows) or rows[row_index].get("resolution") != "pending":
        raise ImportError("Phonebook review target is unavailable.")
    row = rows[row_index]
    candidates = row.get("candidates", [])
    index = int(row.get("candidate_index", 0))
    if action == "next":
        if index + 1 >= len(candidates):
            raise ImportError("No further candidate is available; create or skip this row.")
        row["candidate_index"] = index + 1
    elif action in {"create", "skip"}:
        row["resolution"] = action
    elif action == "update":
        if index >= len(candidates):
            raise ImportError("Select a valid Phonebook candidate.")
        target_id = str(candidates[index]["id"])
        if any(
            other.get("resolution") == "update" and other.get("target_id") == target_id
            for other in rows
        ):
            raise ImportError("This Phonebook entry is already selected by another imported row.")
        row["resolution"], row["target_id"], row["target_fingerprint"] = (
            "update",
            target_id,
            candidates[index]["fingerprint"],
        )
    else:
        raise ImportError("Invalid Phonebook review action.")
    rows[row_index] = row
    batch.normalized_intent["rows"] = rows
    batch.save(update_fields=("normalized_intent",))
    record_event(
        action="ingestion.phonebook_review",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={"row": row_index, "action": action},
    )
    return batch


def _station_vehicle_intent(
    *, rows, department
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Prepare staged Station/Vehicle rows and only review unresolved relationships.

    This deliberately does not create a Station.  It records enough immutable
    input and reviewer-owned resolution state for the final atomic apply.
    """
    intent: list[dict[str, object]] = []
    staged_by_code: dict[str, str] = {}
    staged_by_name: dict[str, str] = {}
    review_items: list[dict[str, object]] = []
    for index, source in enumerate(rows):
        row = dict(source)
        key = f"{row['row_type']}:{index}"
        row["key"] = key
        if row["row_type"] == "station":
            matches = _station_matches(
                department=department,
                short_code=str(row["station_short_code"]),
                name=str(row["station_name"]),
            )
            if len(matches) > 1:
                raise ImportValidationError("Station import matches multiple canonical Stations.")
            row["action"] = "existing" if matches else "new"
            if matches:
                row["station_id"] = str(matches[0].id)
            staged_by_code[_station_reference_key(row["station_short_code"])] = key
            staged_by_name[_station_reference_key(row["station_name"])] = key
        else:
            code_key = _station_reference_key(row["station_short_code"])
            name_key = _station_reference_key(row["station_name"])
            staged_key = staged_by_code.get(code_key) if code_key else staged_by_name.get(name_key)
            if staged_key:
                row["station_resolution"] = {"state": "staged", "station_key": staged_key}
            else:
                matches = _station_matches(
                    department=department,
                    short_code=str(row["station_short_code"]),
                    name=str(row["station_name"]),
                )
                if len(matches) == 1:
                    row["station_resolution"] = {
                        "state": "existing",
                        "station_id": str(matches[0].id),
                    }
                elif len(matches) > 1:
                    row["station_resolution"] = {"state": "ambiguous"}
                    review_items.append(
                        {
                            "key": key,
                            "kind": "ambiguous",
                            "vehicle_name": row["vehicle_name"],
                            "reference": row["station_short_code"] or row["station_name"],
                            "reference_kind": "Short Code"
                            if row["station_short_code"]
                            else "Station name",
                            "candidate_ids": [str(station.id) for station in matches],
                        }
                    )
                else:
                    row["station_resolution"] = {"state": "missing"}
                    review_items.append(
                        {
                            "key": key,
                            "kind": "missing",
                            "vehicle_name": row["vehicle_name"],
                            "reference": row["station_short_code"] or row["station_name"],
                            "reference_kind": "Short Code"
                            if row["station_short_code"]
                            else "Station name",
                        }
                    )
        intent.append(row)
    return intent, review_items


def _preview_station_vehicles(
    *, intent
) -> tuple[tuple[int, int, int, int], list[dict[str, object]]]:
    """Summarize staged creates; imports never retire resources by omission."""
    add = unchanged = 0
    updates: list[dict[str, object]] = []
    for row in intent:
        if row["row_type"] == "station":
            if row.get("action") == "new":
                add += 1
            else:
                unchanged += 1
        else:
            # Existing equivalent vehicles are rechecked while holding locks at
            # Apply; preview is deliberately conservative and staged-only.
            add += 1
        updates.append(
            {
                "key": row["key"],
                "row_type": row["row_type"],
                "label": row.get("station_name") or row.get("vehicle_name"),
            }
        )
    return (add, 0, 0, unchanged), updates


def create_preview(
    *,
    actor,
    department,
    domain: str,
    import_format: str,
    import_mode: str,
    filename: str,
    payload: bytes,
    station=None,
) -> ImportBatch:
    """Stage and parse one exact upload without touching canonical records."""
    require_department_admin(actor, department)
    maximum_bytes = (
        settings.MAX_INGEST_UPLOAD_BYTES
        if domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}
        else settings.MAX_STRUCTURED_IMPORT_BYTES
    )
    if len(payload) > maximum_bytes:
        raise ImportError("Import exceeds the configured source size limit.")
    if station is not None and station.department_id != department.id:
        raise ImportError("Import station is outside the department.")
    batch = ImportBatch(
        domain=domain,
        department=department,
        import_format=import_format,
        import_mode=import_mode,
        original_filename=filename[:255],
        actor=actor,
        station=station,
        staging_key=f"pending-{__import__('uuid').uuid4()}.source",
    )
    document_failures: list[dict[str, str]] = []
    total_documents = 0
    try:
        if domain == ImportBatch.Domain.HYDRANTS:
            if import_mode != ImportBatch.Mode.MERGE:
                raise ImportError("Hydrant imports require merge mode.")
            intent = parse_hydrants(payload=payload, import_format=import_format)
            baseline = _hydrant_baseline(
                department=department, identifiers=_hydrant_identifiers(intent)
            )
            counts, hydrant_updates, updates_truncated = _preview_hydrants(
                intent=intent, department=department
            )
        elif domain == ImportBatch.Domain.PERSONNEL:
            if import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("Personnel imports support upsert mode only.")
            parsed_rows = parse_personnel(payload=payload, import_format=import_format)
            intent, review_items = _personnel_import_intent(
                rows=parsed_rows, department=department, fallback_station=station
            )
            baseline = _personnel_baseline(department=department)
            counts, updates, updates_truncated = _preview_personnel(
                intent=intent, department=department
            )
        elif domain == ImportBatch.Domain.STATION_VEHICLES:
            if import_format != ImportBatch.Format.CSV or import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("Station and Vehicle imports require CSV upsert mode.")
            parsed_rows = parse_station_vehicles(payload=payload, import_format=import_format)
            intent, review_items = _station_vehicle_intent(rows=parsed_rows, department=department)
            baseline = _station_vehicle_baseline(department=department)
            counts, updates = _preview_station_vehicles(intent=intent)
            updates_truncated = False
        elif domain == ImportBatch.Domain.PHONEBOOK:
            if import_format != ImportBatch.Format.CSV or import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("Phonebook imports require CSV upsert mode.")
            intent, review_items = _phonebook_intent(
                rows=parse_phonebook(payload=payload, import_format=import_format),
                department=department,
            )
            baseline = _phonebook_baseline(department=department)
            counts = (sum(row["resolution"] == "create" for row in intent), 0, 0, 0)
            updates, updates_truncated = [], False
        elif domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
            if import_format != ImportBatch.Format.ZIP or import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("PDF package imports require ZIP upsert mode.")
            intent, document_failures, total_documents = _sanitize_pdf_preview(
                batch=batch, payload=payload, department=department, domain=domain
            )
            if document_failures and not intent:
                raise ImportError("No documents in this package were accepted.")
            baseline = _document_baseline(department=department, domain=domain)
            counts, updates, updates_truncated, review_items = _preview_documents(
                intent=intent, department=department, domain=domain
            )
        else:
            raise ImportError("This import domain is not available through structured ingestion.")
    except (ImportValidationError, ImportError) as error:
        batch.status = ImportBatch.Status.INVALID
        batch.validation_errors = [{"code": "invalid_import", "message": str(error)}]
        batch.validation_summary = {"error_count": 1}
        batch.upload_sha256 = hashlib.sha256(payload).hexdigest()
        batch.save()
        record_event(
            action="ingestion.preview_failed",
            actor_user=actor,
            department=department,
            target_type="import_batch",
            target_uuid=batch.id,
            metadata={
                "domain": domain,
                "format": import_format,
                "source_sha256": batch.upload_sha256,
            },
        )
        return batch
    try:
        key, digest = stage_upload(batch_id=batch.id, payload=payload)
    except ImportStorageError as error:
        for row in intent:
            if isinstance(row, dict) and (sanitized_key := row.get("sanitized_staging_key")):
                remove_staged(key=str(sanitized_key))
        raise ImportError("Import source could not be staged.") from error
    batch.staging_key = key
    batch.upload_sha256 = digest
    batch.baseline = baseline
    batch.normalized_intent = {"rows": intent}
    batch.status = ImportBatch.Status.PREVIEW_READY
    batch.previewed_at = timezone.now()
    batch.add_count, batch.update_count, batch.deactivate_count, batch.unchanged_count = counts
    batch.validation_summary = {"error_count": 0, "row_count": len(intent)}
    if domain == ImportBatch.Domain.HYDRANTS:
        batch.validation_summary["updates"] = hydrant_updates
    elif domain in {
        ImportBatch.Domain.PERSONNEL,
        ImportBatch.Domain.FIRE_PLANS,
        ImportBatch.Domain.KLGV_PLANS,
        ImportBatch.Domain.STATION_VEHICLES,
        ImportBatch.Domain.PERSONNEL,
    }:
        batch.validation_summary["updates"] = updates
    batch.validation_summary["updates_truncated"] = updates_truncated
    if domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        batch.validation_summary["review_items"] = review_items
        batch.validation_summary["review_decisions"] = {}
        batch.validation_summary["skipped_update_count"] = 0
    if domain == ImportBatch.Domain.PERSONNEL:
        batch.validation_summary["review_items"] = review_items
        batch.validation_summary["review_decisions"] = {}
        batch.validation_summary["skipped_update_count"] = 0
    if domain == ImportBatch.Domain.STATION_VEHICLES:
        batch.validation_summary["review_items"] = review_items
        batch.validation_summary["review_decisions"] = {}
        batch.validation_summary["skipped_update_count"] = 0
    if domain == ImportBatch.Domain.PHONEBOOK:
        batch.validation_summary["review_items"] = review_items
    if domain == ImportBatch.Domain.FIRE_PLANS:
        batch.validation_summary["coordinate_conflicts"] = _coordinate_conflicts(
            department=department, intent=intent
        )
    if domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        batch.validation_summary["document_failures"] = document_failures
        batch.validation_summary["total_document_count"] = total_documents
        batch.validation_summary["ready_document_count"] = len(intent)
        batch.validation_summary["rejected_document_count"] = len(document_failures)
    batch.save()
    record_event(
        action="ingestion.preview_created",
        actor_user=actor,
        department=department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={
            "domain": domain,
            "format": import_format,
            "mode": import_mode,
            "source_sha256": digest,
            "add": counts[0],
            "update": counts[1],
            "deactivate": counts[2],
            "unchanged": counts[3],
            "ready_documents": total_documents - len(document_failures),
            "rejected_documents": len(document_failures),
            "rejection_codes": ",".join(failure["code"] for failure in document_failures),
        },
    )
    return batch


def _preview_hydrants(*, intent, department):
    identifiers = _hydrant_identifiers(intent)
    existing = {
        hydrant.external_identifier: hydrant
        for hydrant in Hydrant.objects.filter(
            department=department, external_identifier__in=identifiers
        )
    }
    add = update = unchanged = 0
    details: list[dict[str, object]] = []
    for row in intent:
        current = existing.get(row["external_identifier"])
        if current is None:
            add += 1
        else:
            changed_fields = _hydrant_changed_fields(current=current, proposed=row)
            if changed_fields:
                update += 1
                if len(details) < settings.MAX_IMPORT_VALIDATION_ERRORS:
                    current_values = _hydrant_business_values(current)
                    proposed_values = _hydrant_business_values(row)
                    details.append(
                        {
                            "external_identifier": row["external_identifier"],
                            "fields": [
                                {
                                    "name": field,
                                    "current": current_values[field],
                                    "proposed": proposed_values[field],
                                }
                                for field in changed_fields
                            ],
                        }
                    )
            else:
                unchanged += 1
    # Hydrant lifecycle deactivation is explicit only: absence from an import
    # never deactivates; an explicit ``status=INACTIVE`` is an update.
    return (add, update, 0, unchanged), details, update > len(details)


def _personnel_changed(*, current: Person, proposed: dict[str, object]) -> bool:
    return any(
        getattr(current, field) != proposed[field]
        for field in ("personnel_number", "first_name", "last_name", "incident_commander_eligible")
    )


def _preview_personnel(*, intent, department):
    existing = {
        person.personnel_number: person
        for person in Person.objects.filter(
            department=department,
            personnel_number__isnull=False,
            lifecycle_status=Person.LifecycleStatus.ACTIVE,
        )
    }
    add = update = unchanged = 0
    details: list[dict[str, object]] = []
    for row in intent:
        current = existing.get(row["personnel_number"])
        if current is None:
            add += 1
        elif _personnel_changed(current=current, proposed=row):
            update += 1
            if len(details) < settings.MAX_IMPORT_VALIDATION_ERRORS:
                fields = [
                    field
                    for field in ("first_name", "last_name", "incident_commander_eligible")
                    if getattr(current, field) != row[field]
                ]
                details.append(
                    {
                        "external_identifier": row["personnel_number"],
                        "fields": [
                            {
                                "name": field,
                                "current": getattr(current, field),
                                "proposed": row[field],
                            }
                            for field in fields
                        ],
                    }
                )
        else:
            unchanged += 1
    return (add, update, 0, unchanged), details, update > len(details)


@transaction.atomic
def apply_preview(*, actor, batch_id) -> ImportBatch:
    """Apply exactly the staged, still-current preview once."""
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Import batch is not confirmable.")
    try:
        payload = read_staged(key=batch.staging_key)
    except ImportStorageError as error:
        _fail_batch(batch, "staging_unavailable")
        raise ImportError("Preview source is unavailable; create a new preview.") from error
    if hashlib.sha256(payload).hexdigest() != batch.upload_sha256:
        _fail_batch(batch, "staging_hash_mismatch")
        raise ImportError("Preview source changed; create a new preview.")
    try:
        if batch.domain == ImportBatch.Domain.HYDRANTS:
            rows = parse_hydrants(payload=payload, import_format=batch.import_format)
            if (
                _hydrant_baseline(
                    department=batch.department, identifiers=_hydrant_identifiers(rows)
                )
                != batch.baseline
            ):
                raise ImportError("Canonical hydrants changed; re-preview is required.")
            scopes, counts = _apply_hydrants(batch=batch, rows=rows)
        elif batch.domain == ImportBatch.Domain.PERSONNEL:
            # Re-parse only to prove the staged source still satisfies the
            # documented CSV contract. Review-owned home-station resolutions
            # live in normalized_intent and are applied only here.
            parse_personnel(payload=payload, import_format=batch.import_format)
            if _personnel_baseline(department=batch.department) != batch.baseline:
                raise ImportError("Canonical personnel changed; re-preview is required.")
            scopes, counts = _apply_personnel(
                batch=batch, rows=batch.normalized_intent.get("rows", [])
            )
        elif batch.domain == ImportBatch.Domain.STATION_VEHICLES:
            # Re-parse the staged source to prove it is still a valid instance
            # of the documented CSV contract. Reviewer resolutions live in the
            # separately hash-bound normalized intent.
            parse_station_vehicles(payload=payload, import_format=batch.import_format)
            if _station_vehicle_baseline(department=batch.department) != batch.baseline:
                raise ImportError("Canonical Stations or Vehicles changed; re-preview is required.")
            scopes, counts = _apply_station_vehicles(batch=batch, actor=actor)
        elif batch.domain == ImportBatch.Domain.PHONEBOOK:
            parse_phonebook(payload=payload, import_format=batch.import_format)
            if _phonebook_baseline(department=batch.department) != batch.baseline:
                raise ImportError("Canonical Phonebook entries changed; re-preview is required.")
            scopes, counts = _apply_phonebook(batch=batch, actor=actor)
        elif batch.domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
            # Reconstructing package structure proves that stored preview bytes
            # still match the staged source; accepted PDF outputs are separately
            # hash-bound in normalized_intent.
            parse_pdf_package(payload=payload, domain=batch.domain)
            if (
                _document_baseline(department=batch.department, domain=batch.domain)
                != batch.baseline
            ):
                raise ImportError("Canonical documents changed; re-preview is required.")
            scopes, counts = _apply_documents(batch=batch)
        else:
            raise ImportError("This import domain is unavailable.")
    except (ImportValidationError, ImportError):
        raise
    for dataset_type_code, station in scopes:
        mark_dirty(
            department=batch.department,
            station=station,
            dataset_type_code=dataset_type_code,
            actor=actor,
        )
    batch.status = ImportBatch.Status.APPLIED
    batch.applied_at = timezone.now()
    batch.add_count, batch.update_count, batch.deactivate_count, batch.unchanged_count = counts
    batch.affected_scopes = [
        {"dataset_type_code": code, "station_id": str(station.id) if station else None}
        for code, station in scopes
    ]
    batch.save(
        update_fields=(
            "status",
            "applied_at",
            "add_count",
            "update_count",
            "deactivate_count",
            "unchanged_count",
            "affected_scopes",
            "validation_summary",
        )
    )
    record_event(
        action="ingestion.applied",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={
            "domain": batch.domain,
            "source_sha256": batch.upload_sha256,
            "add": counts[0],
            "update": counts[1],
            "deactivate": counts[2],
            "unchanged": counts[3],
            "affected_scope_count": len(scopes),
            "skipped_updates": batch.validation_summary.get("skipped_update_count", 0),
            "rejected_documents": len(batch.validation_summary.get("document_failures", [])),
            "rejection_codes": ",".join(
                failure["code"] for failure in batch.validation_summary.get("document_failures", [])
            ),
        },
    )
    return batch


def _apply_phonebook(*, batch, actor):
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    if any(row.get("resolution") == "pending" for row in rows):
        raise ImportError("Resolve each Phonebook duplicate before applying this import.")
    add = update = unchanged = 0
    for row in rows:
        resolution = row.get("resolution")
        if resolution == "skip":
            unchanged += 1
            continue
        station = None
        if row.get("station_id"):
            station = (
                Station.objects.select_for_update()
                .filter(id=row["station_id"], department=batch.department)
                .first()
            )
            if station is None:
                raise ImportError("A Phonebook scope changed; re-preview is required.")
        values = {
            field: row.get(field, "")
            for field in (
                "first_name",
                "last_name",
                "organization_unit",
                "function",
                "phone_number",
            )
        }
        values["station"] = station
        if resolution == "create":
            create_phonebook_entry(actor=actor, department=batch.department, **values)
            add += 1
        elif resolution == "update":
            entry = (
                PhonebookEntry.objects.select_for_update()
                .filter(id=row.get("target_id"), department=batch.department)
                .first()
            )
            if entry is None or entry_fingerprint(entry) != row.get("target_fingerprint"):
                raise ImportError("A selected Phonebook entry changed; re-preview is required.")
            update_phonebook_entry(actor=actor, entry=entry, **values)
            update += 1
        else:
            raise ImportError("Phonebook reconciliation is incomplete.")
    return [], (add, update, 0, unchanged)


def _fail_batch(batch, code):
    batch.status = ImportBatch.Status.FAILED
    batch.failed_at = timezone.now()
    batch.validation_errors = [{"code": code, "message": "Preview must be recreated."}]
    batch.save(update_fields=("status", "failed_at", "validation_errors"))


@transaction.atomic
def cancel_preview(*, actor, batch_id) -> ImportBatch:
    """Cancel a preview once; canonical rows and publication state are untouched."""
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be cancelled.")
    batch.status = ImportBatch.Status.CANCELLED
    batch.cancelled_at = timezone.now()
    batch.save(update_fields=("status", "cancelled_at"))
    record_event(
        action="ingestion.cancelled",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={"domain": batch.domain, "source_sha256": batch.upload_sha256},
    )
    keys = [
        batch.staging_key,
        *[
            row.get("sanitized_staging_key", "")
            for row in batch.normalized_intent.get("rows", [])
            if isinstance(row, dict)
        ],
    ]

    def remove_cancelled_staging() -> None:
        for key in keys:
            if key:
                remove_staged(key=key)

    transaction.on_commit(remove_cancelled_staging)
    return batch


def _apply_hydrants(*, batch, rows):
    identifiers = _hydrant_identifiers(rows)
    existing = {
        hydrant.external_identifier: hydrant
        for hydrant in Hydrant.objects.select_for_update().filter(
            department=batch.department, external_identifier__in=identifiers
        )
    }
    add = update = unchanged = 0
    to_create: list[Hydrant] = []
    to_update: list[Hydrant] = []
    now = timezone.now()
    for row in rows:
        identifier = row["external_identifier"]
        values = {
            "geometry": Point(row["longitude"], row["latitude"], srid=4326),
            "street": row["street"],
            "house_number": row["house_number"],
            "location": row["location"],
            "hydrant_type": row["hydrant_type"],
            "diameter_mm": row["diameter_mm"],
            "status": row["status"],
        }
        hydrant = existing.get(identifier)
        if hydrant is None:
            to_create.append(
                Hydrant(
                    department=batch.department,
                    external_identifier=identifier,
                    source_metadata={},
                    created_at=now,
                    updated_at=now,
                    **values,
                )
            )
            add += 1
        elif _hydrant_changed_fields(current=hydrant, proposed=row):
            for field, value in values.items():
                setattr(hydrant, field, value)
            hydrant.updated_at = now
            to_update.append(hydrant)
            update += 1
        else:
            unchanged += 1
    if to_create:
        Hydrant.objects.bulk_create(to_create, batch_size=_INGESTION_BULK_BATCH_SIZE)
    if to_update:
        Hydrant.objects.bulk_update(
            to_update,
            fields=(
                "geometry",
                "street",
                "house_number",
                "location",
                "hydrant_type",
                "diameter_mm",
                "status",
                "updated_at",
            ),
            batch_size=_INGESTION_BULK_BATCH_SIZE,
        )
    # Hydrant lifecycle deactivation is explicit only: absence from an import
    # never deactivates a record. Deactivation is an update where the imported
    # row explicitly sets status=INACTIVE.
    scopes = [("department_hydrants", None)] if (add or update) else []
    return scopes, (add, update, 0, unchanged)


def _apply_personnel(*, batch, rows):
    existing = {
        person.personnel_number: person
        for person in Person.objects.select_for_update().filter(
            department=batch.department,
            personnel_number__isnull=False,
            lifecycle_status=Person.LifecycleStatus.ACTIVE,
        )
    }
    if _review_summary(batch)["pending"]:
        raise ImportError("Resolve each Home Station review item before applying this import.")
    decisions = _review_decisions(batch)
    add = update = unchanged = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ImportError("Personnel review data is unavailable.")
        key = str(row.get("key", ""))
        if decisions.get(key) == "skipped":
            unchanged += 1
            continue
        person = existing.get(row["personnel_number"])
        display_name = f"{row['first_name']} {row['last_name']}".strip()
        resolution = row.get("home_station_resolution", {})
        state = resolution.get("state") if isinstance(resolution, dict) else None
        home_station = (
            Station.objects.filter(
                pk=resolution.get("station_id"), department=batch.department, active=True
            ).first()
            if state == "existing"
            else None
        )
        if person is None:
            if home_station is None:
                raise ImportError("New personnel require an active Home Station.")
            person = create_person(
                actor=batch.actor,
                department=batch.department,
                home_station=home_station,
                personnel_number=str(row["personnel_number"]),
                first_name=str(row["first_name"]),
                last_name=str(row["last_name"]),
                incident_commander_eligible=bool(row["incident_commander_eligible"]),
            )
            add += 1
        else:
            changed = False
            if (
                _personnel_changed(current=person, proposed=row)
                or person.display_name != display_name
            ):
                person = update_person(
                    actor=batch.actor,
                    person=person,
                    personnel_number=str(row["personnel_number"]),
                    first_name=str(row["first_name"]),
                    last_name=str(row["last_name"]),
                )
                changed = True
            if person.incident_commander_eligible != bool(row["incident_commander_eligible"]):
                person = set_commander_eligibility(
                    actor=batch.actor,
                    person=person,
                    eligible=bool(row["incident_commander_eligible"]),
                )
                changed = True
            if home_station is not None:
                current_home_id = (
                    PersonnelStationAssignment.objects.filter(
                        person=person,
                        assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
                        ended_at__isnull=True,
                        valid_until__isnull=True,
                    )
                    .values_list("station_id", flat=True)
                    .first()
                )
                if current_home_id != home_station.id:
                    transfer_home(person=person, station=home_station, actor=batch.actor)
                    changed = True
            if changed:
                update += 1
            else:
                unchanged += 1
    # Personnel services already mark every affected current/previous HOME
    # scope exactly according to the authoritative assignment semantics.
    return [], (add, update, 0, unchanged)


def _staged_station_values(row: dict[str, object]) -> dict[str, str]:
    return {
        field: str(row.get(field, "") or "").strip()
        for field in ("short_code", "name", "street", "house_number", "postal_code", "city")
    }


def _apply_station_vehicles(*, batch, actor):
    """Create accepted Station/Vehicle rows atomically through canonical services."""
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    decisions = _review_decisions(batch)
    pending = _review_summary(batch)["pending"]
    if pending:
        raise ImportError("Resolve each Station relationship before applying this import.")

    # Serialize same-department import applies before checking non-database
    # normalized identifiers. The canonical models intentionally do not impose
    # a case-insensitive Short Code/name constraint.
    department = type(batch.department).objects.select_for_update().get(pk=batch.department_id)
    stations = list(Station.objects.select_for_update().filter(department=department))
    station_by_key: dict[str, Station] = {}
    existing_code_keys = {_station_reference_key(station.short_code) for station in stations}
    existing_name_keys = {_station_reference_key(station.name) for station in stations}
    add = unchanged = 0

    for row in rows:
        if row.get("row_type") != "station":
            continue
        key = str(row["key"])
        if row.get("action") == "existing":
            station = next(
                (station for station in stations if str(station.id) == str(row.get("station_id"))),
                None,
            )
            if station is None:
                raise ImportError("A referenced Station changed; re-preview is required.")
            station_by_key[key] = station
            continue
        values = {
            "short_code": str(row["station_short_code"]),
            "name": str(row["station_name"]),
            "street": str(row.get("street", "")),
            "house_number": str(row.get("house_number", "")),
            "postal_code": str(row.get("postal_code", "")),
            "city": str(row.get("city", "")),
        }
        code_key, name_key = (
            _station_reference_key(values["short_code"]),
            _station_reference_key(values["name"]),
        )
        if code_key in existing_code_keys or name_key in existing_name_keys:
            raise ImportError(
                "A Station already uses this Short Code or name; re-preview is required."
            )
        station = create_station(actor=actor, department=department, **values)
        stations.append(station)
        station_by_key[key] = station
        existing_code_keys.add(code_key)
        existing_name_keys.add(name_key)
        add += 1

    # Missing-Stations approved in review are created after CSV Station rows,
    # but before their dependent Vehicles, in this same outer transaction.
    for row in rows:
        if row.get("row_type") != "vehicle":
            continue
        resolution = dict(row.get("station_resolution", {}))
        if resolution.get("state") != "staged" or not str(
            resolution.get("station_key", "")
        ).startswith("resolution:"):
            continue
        resolution_key = str(resolution["station_key"])
        if resolution_key in station_by_key:
            continue
        staged = next(
            (
                candidate
                for candidate in batch.normalized_intent.get("staged_stations", [])
                if isinstance(candidate, dict) and candidate.get("key") == resolution_key
            ),
            None,
        )
        if staged is None:
            raise ImportError("The staged missing Station resolution is unavailable.")
        values = _staged_station_values(staged)
        code_key, name_key = (
            _station_reference_key(values["short_code"]),
            _station_reference_key(values["name"]),
        )
        if not values["short_code"] or not values["name"]:
            raise ImportError("The staged missing Station is incomplete.")
        if code_key in existing_code_keys or name_key in existing_name_keys:
            raise ImportError(
                "A Station already uses this Short Code or name; re-preview is required."
            )
        station = create_station(actor=actor, department=department, **values)
        stations.append(station)
        station_by_key[resolution_key] = station
        existing_code_keys.add(code_key)
        existing_name_keys.add(name_key)
        add += 1

    existing_vehicles = {
        (vehicle.station_id, _station_reference_key(vehicle.display_name)): vehicle
        for vehicle in Vehicle.objects.select_for_update().filter(department=department)
    }
    for row in rows:
        if row.get("row_type") != "vehicle":
            continue
        key = str(row["key"])
        if decisions.get(key) == "skipped":
            unchanged += 1
            continue
        resolution = dict(row.get("station_resolution", {}))
        state = resolution.get("state")
        if state == "existing":
            station = next(
                (
                    candidate
                    for candidate in stations
                    if str(candidate.id) == str(resolution.get("station_id"))
                ),
                None,
            )
        elif state == "staged":
            station = station_by_key.get(str(resolution.get("station_key")))
        else:
            station = None
        if station is None or station.department_id != department.id:
            raise ImportError("A Vehicle Station is unresolved; re-preview is required.")
        vehicle_key = (station.id, _station_reference_key(row.get("vehicle_name")))
        if vehicle_key in existing_vehicles:
            unchanged += 1
            continue
        vehicle = create_vehicle(
            actor=actor,
            department=department,
            station=station,
            display_name=str(row["vehicle_name"]),
            call_sign=str(row.get("vehicle_call_sign", "")),
            asset_identifier=str(row.get("vehicle_asset_identifier", "")),
        )
        existing_vehicles[vehicle_key] = vehicle
        add += 1
    # Stations and Vehicles do not themselves create or dirty a distributed
    # dataset. Existing downstream side effects remain exclusively in the
    # canonical services invoked above.
    return [], (add, 0, 0, unchanged)


def _document_identity_key(document_or_row, *, domain: str) -> str:
    """Return the canonical import identity, never storage/persistence metadata."""
    external_identifier = str(
        document_or_row["external_identifier"]
        if isinstance(document_or_row, dict)
        else document_or_row.external_identifier
    ).strip()
    if domain == ImportBatch.Domain.KLGV_PLANS:
        if external_identifier:
            return f"external_identifier:{external_identifier}"
        object_name = str(
            document_or_row["title"]
            if isinstance(document_or_row, dict)
            else document_or_row.object_name
        ).strip()
        address = str(
            document_or_row["address"]
            if isinstance(document_or_row, dict)
            else document_or_row.address
        ).strip()
        return f"object_name_address:{object_name}\x00{address}"
    if domain != ImportBatch.Domain.FIRE_PLANS:
        return f"external_identifier:{external_identifier}"
    address = str(
        document_or_row["address"] if isinstance(document_or_row, dict) else document_or_row.address
    ).strip()
    if external_identifier:
        return f"external_identifier:{external_identifier}"
    return f"address:{address}"


def _identity_match(row: dict[str, object], *, domain: str) -> dict[str, str]:
    """Describe which identity rule matched an incoming row (for the review wizard)."""
    external_identifier = str(row.get("external_identifier", "") or "").strip()
    if domain == ImportBatch.Domain.KLGV_PLANS:
        if external_identifier:
            return {"strategy": "external_identifier", "value": external_identifier}
        return {
            "strategy": "object_name_address",
            "value": f"{row.get('title', '')} · {row.get('address', '')}",
        }
    if domain != ImportBatch.Domain.FIRE_PLANS:
        return {"strategy": "external_identifier", "value": external_identifier}
    address = str(row.get("address", "") or "").strip()
    if external_identifier:
        return {"strategy": "external_identifier", "value": external_identifier}
    return {"strategy": "address_fallback", "value": address}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Geodesic distance in kilometres (mean-earth-radius haversine)."""
    import math

    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _mib_format(value: int) -> str:
    return f"{value / (1024 * 1024):.2f} MiB"


def _document_baseline(*, department, domain: str) -> dict[str, str]:
    if domain == ImportBatch.Domain.FIRE_PLANS:
        rows = FirePlan.objects.filter(department=department).only(
            "external_identifier",
            "updated_at",
            "active",
            "sha256",
            "object_name",
            "address",
            "postal_code",
            "city",
            "fsd_location",
            "bmz_location",
            "rwa_info",
        )
        return {
            _document_identity_key(row, domain=domain): _fingerprint(
                {
                    "updated_at": row.updated_at.isoformat(),
                    "active": row.active,
                    "sha256": row.sha256,
                    "object_name": row.object_name,
                    "address": row.address,
                    "postal_code": row.postal_code,
                    "city": row.city,
                    "fsd_location": row.fsd_location,
                    "bmz_location": row.bmz_location,
                    "rwa_info": row.rwa_info,
                }
            )
            for row in rows
        }
    klgv_rows = KlgvPlan.objects.filter(department=department).only(
        "external_identifier",
        "updated_at",
        "active",
        "sha256",
        "object_name",
        "address",
        "postal_code",
        "city",
        "location",
    )
    return {
        _document_identity_key(row, domain=domain): _fingerprint(
            {
                "updated_at": row.updated_at.isoformat(),
                "active": row.active,
                "sha256": row.sha256,
                "object_name": row.object_name,
                "address": row.address,
                "postal_code": row.postal_code,
                "city": row.city,
                "location": (
                    {"longitude": row.location.x, "latitude": row.location.y}
                    if row.location is not None
                    else None
                ),
            }
        )
        for row in klgv_rows
    }


def _preview_documents(*, intent, department, domain):
    model = FirePlan if domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    existing = {
        _document_identity_key(row, domain=domain): row
        for row in model.objects.filter(department=department)
    }
    add = update = deactivate = unchanged = 0
    details: list[dict[str, object]] = []
    review_items: list[dict[str, object]] = []
    for row in intent:
        current = existing.get(_document_identity_key(row, domain=domain))
        if row["action"] == "deactivate":
            if current is not None and current.active:
                deactivate += 1
            else:
                unchanged += 1
            continue
        if current is None:
            add += 1
        elif _document_content_changed(
            current=current, row=row, model=model
        ) or _document_metadata_changes(current=current, row=row, model=model):
            update += 1
            detail = _document_update_detail(current=current, row=row, model=model, domain=domain)
            review_items.append(
                {field: value for field, value in detail.items() if field != "fields"}
            )
            if len(details) < settings.MAX_IMPORT_VALIDATION_ERRORS:
                details.append(detail)
        else:
            unchanged += 1
    return (add, update, deactivate, unchanged), details, update > len(details), review_items


def _document_update_detail(*, current, row, model, domain) -> dict[str, object]:
    """Build a review-wizard explanation for one matched canonical update."""
    match = _identity_match(row, domain=domain)
    return {
        "key": str(current.id),
        "external_identifier": row["external_identifier"],
        "matched_record_id": str(current.id),
        "identity_strategy": match["strategy"],
        "matched_value": match["value"],
        "incoming_filename": row.get("original_filename", ""),
        "fields": _document_changed_fields(current=current, row=row, model=model),
    }


_REVIEW_DECISIONS = ("approved", "skipped")


def _review_keys(batch: ImportBatch) -> list[str]:
    """Return the ordered identity keys of every proposed document update."""
    return [
        str(item["key"])
        for item in batch.validation_summary.get("review_items", [])
        if isinstance(item, dict) and item.get("key")
    ]


def _review_decisions(batch: ImportBatch) -> dict[str, str]:
    """Return the persisted per-update decisions (``approved``/``skipped`` only)."""
    raw = batch.validation_summary.get("review_decisions", {})
    return {
        str(key): str(value["decision"])
        for key, value in raw.items()
        if isinstance(value, dict) and value.get("decision") in _REVIEW_DECISIONS
    }


def _review_summary(batch: ImportBatch) -> dict[str, int]:
    keys = _review_keys(batch)
    decisions = _review_decisions(batch)
    approved = sum(1 for key in keys if decisions.get(key) == "approved")
    skipped = sum(1 for key in keys if decisions.get(key) == "skipped")
    return {
        "total": len(keys),
        "pending": len(keys) - approved - skipped,
        "approved": approved,
        "skipped": skipped,
    }


@transaction.atomic
def set_review_decision(*, actor, batch_id, key: str, decision: str) -> ImportBatch:
    """Record one approve/skip decision for a proposed document update.

    Decisions persist on the batch (JSON, no migration) and are therefore bound to
    the exact staged preview; any canonical mutation after review re-triggers the
    stale-baseline check in ``apply_preview``.
    """
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be reviewed.")
    if batch.domain not in {
        ImportBatch.Domain.FIRE_PLANS,
        ImportBatch.Domain.KLGV_PLANS,
        ImportBatch.Domain.STATION_VEHICLES,
        ImportBatch.Domain.PERSONNEL,
    }:
        raise ImportError("Update review is not available for this import domain.")
    if decision not in _REVIEW_DECISIONS:
        raise ImportError("Invalid review decision.")
    if key not in _review_keys(batch):
        raise ImportError("Review target is not a proposed update.")
    if batch.domain == ImportBatch.Domain.STATION_VEHICLES and decision == "approved":
        row = next(
            (
                row
                for row in batch.normalized_intent.get("rows", [])
                if isinstance(row, dict) and str(row.get("key")) == key
            ),
            None,
        )
        if not isinstance(row, dict) or row.get("station_resolution", {}).get("state") not in {
            "existing",
            "staged",
        }:
            raise ImportError("Resolve the Vehicle Station before accepting this change.")
    if batch.domain == ImportBatch.Domain.PERSONNEL and decision == "approved":
        row = next(
            (
                row
                for row in batch.normalized_intent.get("rows", [])
                if isinstance(row, dict) and str(row.get("key")) == key
            ),
            None,
        )
        if not isinstance(row, dict) or row.get("home_station_resolution", {}).get("state") not in {
            "existing",
            "retain",
        }:
            raise ImportError("Resolve the Home Station before accepting this change.")
    decisions = dict(batch.validation_summary.get("review_decisions", {}))
    decisions[key] = {
        "decision": decision,
        "decided_at": timezone.now().isoformat(),
        "decided_by": str(actor.id),
    }
    batch.validation_summary["review_decisions"] = decisions
    batch.save(update_fields=("validation_summary",))
    record_event(
        action=f"ingestion.review_{decision}",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={"domain": batch.domain, "review_key": key},
    )
    return batch


@transaction.atomic
def set_station_vehicle_resolution(
    *, actor, batch_id, key: str, resolution_kind: str, values: dict[str, object]
) -> ImportBatch:
    """Persist a reviewer-selected Station relationship as staged batch state.

    The operation is intentionally not a canonical Station mutation.  It only
    makes the accepted Vehicle eligible for the later atomic Apply operation.
    """
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be reviewed.")
    if batch.domain != ImportBatch.Domain.STATION_VEHICLES:
        raise ImportError("Station resolution is only available for Station and Vehicle imports.")
    item = next(
        (
            item
            for item in batch.validation_summary.get("review_items", [])
            if isinstance(item, dict) and str(item.get("key")) == key
        ),
        None,
    )
    if item is None or item.get("kind") != resolution_kind:
        raise ImportError("Station review target is unavailable.")
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    row = next((candidate for candidate in rows if str(candidate.get("key")) == key), None)
    if row is None:
        raise ImportError("Station review data is unavailable.")
    if resolution_kind == "ambiguous":
        station = values.get("station_id")
        if (
            not isinstance(station, Station)
            or station.department_id != batch.department_id
            or not station.active
        ):
            raise ImportError("Choose an active Station in this Department.")
        candidate_ids = {str(value) for value in item.get("candidate_ids", [])}
        if str(station.id) not in candidate_ids:
            raise ImportError("Choose one of the matching Department Stations.")
        row["station_resolution"] = {"state": "existing", "station_id": str(station.id)}
    else:
        resolution_key = f"resolution:{key}"
        staged = {
            "key": resolution_key,
            "short_code": str(values.get("short_code", "")).strip(),
            "name": str(values.get("name", "")).strip(),
            "street": str(values.get("street", "")).strip(),
            "house_number": str(values.get("house_number", "")).strip(),
            "postal_code": str(values.get("postal_code", "")).strip(),
            "city": str(values.get("city", "")).strip(),
        }
        if not staged["short_code"] or not staged["name"]:
            raise ImportError("Short Code and Station name are required.")
        staged_stations = [
            dict(candidate)
            for candidate in batch.normalized_intent.get("staged_stations", [])
            if isinstance(candidate, dict) and candidate.get("key") != resolution_key
        ]
        staged_stations.append(staged)
        batch.normalized_intent["staged_stations"] = staged_stations
        row["station_resolution"] = {"state": "staged", "station_key": resolution_key}
    batch.normalized_intent["rows"] = rows
    batch.save(update_fields=("normalized_intent",))
    # This records the decision, audit event, and review timestamp under the
    # same row lock.  The nested atomic block is safe and keeps both writes
    # transactional.
    return set_review_decision(actor=actor, batch_id=batch.id, key=key, decision="approved")


@transaction.atomic
def set_personnel_home_station_resolution(
    *, actor, batch_id, key: str, station: Station
) -> ImportBatch:
    """Stage an explicit same-department resolution for an ambiguous home station."""
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be reviewed.")
    if batch.domain != ImportBatch.Domain.PERSONNEL:
        raise ImportError("Home Station resolution is only available for Personnel imports.")
    item = next(
        (
            item
            for item in batch.validation_summary.get("review_items", [])
            if isinstance(item, dict) and str(item.get("key")) == key
        ),
        None,
    )
    candidate_ids = {str(candidate) for candidate in (item or {}).get("candidate_ids", [])}
    if not item or item.get("kind") != "personnel_ambiguous_home_station":
        raise ImportError("Home Station review target is unavailable.")
    if (
        station.department_id != batch.department_id
        or not station.active
        or str(station.id) not in candidate_ids
    ):
        raise ImportError("Choose one of the matching active Department Stations.")
    rows = [dict(row) for row in batch.normalized_intent.get("rows", []) if isinstance(row, dict)]
    row = next((candidate for candidate in rows if str(candidate.get("key")) == key), None)
    if row is None:
        raise ImportError("Home Station review data is unavailable.")
    row["home_station_resolution"] = {"state": "existing", "station_id": str(station.id)}
    batch.normalized_intent["rows"] = rows
    batch.save(update_fields=("normalized_intent",))
    return set_review_decision(actor=actor, batch_id=batch.id, key=key, decision="approved")


@transaction.atomic
def approve_all_review_decisions(*, actor, batch_id) -> ImportBatch:
    """Approve every pending update; requires an explicit UI confirmation."""
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be reviewed.")
    if batch.domain not in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        raise ImportError("Update review is only available for document imports.")
    decisions = dict(batch.validation_summary.get("review_decisions", {}))
    now = timezone.now().isoformat()
    actor_id = str(actor.id)
    for key in _review_keys(batch):
        decisions[key] = {"decision": "approved", "decided_at": now, "decided_by": actor_id}
    batch.validation_summary["review_decisions"] = decisions
    batch.save(update_fields=("validation_summary",))
    record_event(
        action="ingestion.review_approve_all",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={"domain": batch.domain, "approved_count": len(_review_keys(batch))},
    )
    return batch


def review_context(batch: ImportBatch, index: int | None = None) -> dict[str, object]:
    """Build the wizard view context for one document-import preview."""
    items = [
        item for item in batch.validation_summary.get("review_items", []) if isinstance(item, dict)
    ]
    decisions = _review_decisions(batch)
    total = len(items)
    if total:
        if index is None:
            next_pending = next(
                (
                    position
                    for position, item in enumerate(items)
                    if decisions.get(str(item["key"])) not in _REVIEW_DECISIONS
                ),
                None,
            )
            if next_pending is None:
                return {
                    "items": items,
                    "summary": _review_summary(batch),
                    "index": 0,
                    "current": None,
                    "previous_index": None,
                    "next_index": None,
                    "domain": batch.domain,
                    "coordinate_items": _coordinate_review_items(batch),
                }
            index = next_pending
        index = max(0, min(index, total - 1))
    else:
        index = 0
    current: dict[str, object] | None = None
    if total:
        item = dict(items[index])
        key = str(item["key"])
        item["decision"] = decisions.get(key, "pending")
        detail_fields = {
            str(detail["key"]): detail.get("fields", [])
            for detail in batch.validation_summary.get("updates", [])
            if isinstance(detail, dict) and detail.get("key")
        }
        item["fields"] = detail_fields.get(key, [])
        current = item
    return {
        "items": items,
        "summary": _review_summary(batch),
        "index": index,
        "current": current,
        "previous_index": index - 1 if total and index > 0 else None,
        "next_index": index + 1 if total and index < total - 1 else None,
        "domain": batch.domain,
        "coordinate_items": _coordinate_review_items(batch),
    }


def _coordinate_review_items(batch: ImportBatch) -> list[dict[str, object]]:
    if batch.domain not in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        return []
    model = FirePlan if batch.domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    existing_keys = {
        _document_identity_key(plan, domain=batch.domain): str(plan.id)
        for plan in model.objects.filter(department=batch.department).only(
            "id", "external_identifier", "address"
        )
    }
    return [
        {
            "index": row_index,
            "identity": identity,
            "review_key": existing_keys.get(identity),
            "external_identifier": row.get("external_identifier", ""),
            "address": row.get("address", ""),
            "longitude": row.get("longitude"),
            "latitude": row.get("latitude"),
        }
        for row_index, row in enumerate(batch.normalized_intent.get("rows", []))
        if isinstance(row, dict)
        and row.get("action") == "upsert"
        and (row.get("longitude") is None or row.get("latitude") is None)
        for identity in (_document_identity_key(row, domain=batch.domain),)
    ]


@transaction.atomic
def set_review_coordinates(
    *, actor, batch_id, row_index: int, longitude: object, latitude: object
) -> ImportBatch:
    """Store reviewer-supplied Fire Plan coordinates in the staged intent only.

    No canonical row changes until the normal confirmation path applies the
    re-diffed, hash-bound preview.  This keeps manual data-quality completion
    inside the existing ImportBatch transaction/state machine.
    """
    batch = ImportBatch.objects.select_for_update().select_related("department").get(pk=batch_id)
    require_department_admin(actor, batch.department)
    if batch.status != ImportBatch.Status.PREVIEW_READY:
        raise ImportError("Only a ready preview can be corrected.")
    if batch.domain not in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
        raise ImportError("Coordinate completion is only available for document imports.")
    rows = list(batch.normalized_intent.get("rows", []))
    if row_index < 0 or row_index >= len(rows) or not isinstance(rows[row_index], dict):
        raise ImportError("Coordinate review target is unavailable.")
    try:
        parsed_longitude = float(longitude)
        parsed_latitude = float(latitude)
    except (TypeError, ValueError) as error:
        raise ImportError("Longitude and latitude must be numeric.") from error
    if not -180 <= parsed_longitude <= 180:
        raise ImportError("Longitude must be between -180 and 180.")
    if not -90 <= parsed_latitude <= 90:
        raise ImportError("Latitude must be between -90 and 90.")
    row = dict(rows[row_index])
    row["longitude"] = parsed_longitude
    row["latitude"] = parsed_latitude
    rows[row_index] = row
    counts, updates, updates_truncated, review_items = _preview_documents(
        intent=rows,
        department=batch.department,
        domain=batch.domain,
    )
    batch.normalized_intent = {"rows": rows}
    batch.add_count, batch.update_count, batch.deactivate_count, batch.unchanged_count = counts
    batch.validation_summary["updates"] = updates
    batch.validation_summary["updates_truncated"] = updates_truncated
    batch.validation_summary["review_items"] = review_items
    # A changed diff invalidates any prior decision made against the earlier
    # field set; retain decisions only for still-proposed update identities.
    valid_keys = {str(item["key"]) for item in review_items}
    batch.validation_summary["review_decisions"] = {
        key: value
        for key, value in batch.validation_summary.get("review_decisions", {}).items()
        if key in valid_keys
    }
    batch.save(
        update_fields=(
            "normalized_intent",
            "add_count",
            "update_count",
            "deactivate_count",
            "unchanged_count",
            "validation_summary",
        )
    )
    record_event(
        action="ingestion.review_coordinates_completed",
        actor_user=actor,
        department=batch.department,
        target_type="import_batch",
        target_uuid=batch.id,
        metadata={"domain": batch.domain, "row_index": row_index},
    )
    return batch


def _coordinate_conflicts(*, department, intent) -> list[dict[str, object]]:
    existing = {
        _document_identity_key(plan, domain=ImportBatch.Domain.FIRE_PLANS): plan
        for plan in FirePlan.objects.filter(department=department).only(
            "external_identifier", "location"
        )
    }
    conflicts: list[dict[str, object]] = []
    for row in intent:
        current = existing.get(_document_identity_key(row, domain=ImportBatch.Domain.FIRE_PLANS))
        incoming = _location(row)
        if current is None or current.location is None or incoming is None:
            continue
        if current.location != incoming:
            conflicts.append(
                {
                    "external_identifier": row["external_identifier"],
                    "existing": {"latitude": current.location.y, "longitude": current.location.x},
                    "incoming": {"latitude": incoming.y, "longitude": incoming.x},
                }
            )
    return conflicts[: settings.MAX_IMPORT_VALIDATION_ERRORS]


def _sanitize_pdf_preview(
    *, batch: ImportBatch, payload: bytes, department, domain: str
) -> tuple[list[dict[str, object]], list[dict[str, str]], int]:
    """Prepare each document through the quarantine/broker path, keeping outputs staged.

    A structurally valid package supports partial acceptance: a pre-validation
    content/safety rejection (``PdfValidationError``) is skipped and recorded,
    while package/sanitizer-infrastructure failures still abort the whole preview.
    Sanitizer-stage content rejections are only skipped when positively typed
    (``PdfSanitizerContentError``); ambiguous qpdf failures remain fatal.
    Returns ``(rows, failures, total)``.
    """
    entries = parse_pdf_package(payload=payload, domain=domain)
    model = FirePlan if domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    existing = {
        _document_identity_key(row, domain=domain): row
        for row in model.objects.filter(department=department)
    }
    result: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for entry in entries:
            metadata: dict[str, object] = {
                "external_identifier": entry.external_identifier,
                "title": entry.title,
                "address": entry.address,
                "postal_code": entry.postal_code,
                "city": entry.city,
                "fsd_location": entry.fsd_location,
                "bmz_location": entry.bmz_location,
                "rwa_info": entry.rwa_info,
                "category": entry.category,
                "latitude": entry.latitude,
                "longitude": entry.longitude,
                "action": entry.action,
                "original_filename": entry.filename,
            }
            if entry.action == "deactivate":
                result.append(metadata)
                continue
            source_sha256 = hashlib.sha256(entry.pdf_bytes or b"").hexdigest()
            current = existing.get(_document_identity_key(metadata, domain=domain))
            if current is not None and current.source_pdf_sha256 == source_sha256:
                result.append(
                    metadata
                    | {
                        "source_pdf_sha256": source_sha256,
                        "sanitized_pdf_sha256": (
                            current.sha256 if isinstance(current, FirePlan) else current.sha256
                        ),
                        "file_size": current.file_size,
                        "page_count": current.page_count,
                        "content_reused": True,
                    }
                )
                continue
            quarantine = sanitized = None
            stage = "quarantine_write"
            try:
                uploaded = SimpleUploadedFile(
                    entry.filename, entry.pdf_bytes or b"", content_type="application/pdf"
                )
                quarantine = write_quarantine(uploaded)
                stage = "input_validation"
                validate_pdf(quarantine, original_filename=entry.filename)
                stage = "sanitize"
                sanitized = output_path(job_id=quarantine.parent.name)
                sanitize(quarantined_input=quarantine, sanitized_output=sanitized)
                stage = "output_validation"
                file_size, page_count, sanitized_sha256 = validate_pdf(sanitized)
                stage = "staging_copy"
                output_key = f"{batch.id}.{uuid.uuid4()}.sanitized.pdf"
                target = settings.INGESTION_STAGING_ROOT / output_key
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(sanitized, target)
                os.chmod(target, 0o600)
            except PdfSanitizerContentError as error:
                _log_sanitizer_failure(
                    batch=batch,
                    domain=domain,
                    filename=entry.filename,
                    source_sha256=source_sha256,
                    job_uuid=(quarantine.parent.name if quarantine is not None else ""),
                    stage=stage,
                    error=error,
                    input_bytes=len(entry.pdf_bytes or b""),
                )
                failures.append(_document_failure(entry.filename, error))
                continue
            except PdfValidationError as error:
                _log_sanitizer_failure(
                    batch=batch,
                    domain=domain,
                    filename=entry.filename,
                    source_sha256=source_sha256,
                    job_uuid=(quarantine.parent.name if quarantine is not None else ""),
                    stage=stage,
                    error=error,
                    input_bytes=len(entry.pdf_bytes or b""),
                )
                failures.append(_document_failure(entry.filename, error))
                continue
            except (PdfSanitizerError, ReferenceDataStorageError, OSError) as error:
                _log_sanitizer_failure(
                    batch=batch,
                    domain=domain,
                    filename=entry.filename,
                    source_sha256=source_sha256,
                    job_uuid=(quarantine.parent.name if quarantine is not None else ""),
                    stage=stage,
                    error=error,
                    input_bytes=len(entry.pdf_bytes or b""),
                )
                raise ImportError("PDF package was rejected by the PDF safety pipeline.") from error
            finally:
                cleanup(quarantine)
                cleanup(sanitized)
            result.append(
                metadata
                | {
                    "source_pdf_sha256": source_sha256,
                    "sanitized_pdf_sha256": sanitized_sha256,
                    "file_size": file_size,
                    "page_count": page_count,
                    "sanitized_staging_key": output_key,
                }
            )
    except ImportError:
        for row in result:
            if key := row.get("sanitized_staging_key"):
                remove_staged(key=str(key))
        raise
    return result, failures, len(entries)


def _document_failure(filename: str, error: BaseException) -> dict[str, str]:
    """Build a safe, deterministic per-document rejection record."""
    return {
        "filename": filename,
        "code": str(getattr(error, "code", None) or "invalid_pdf"),
        "message": str(error),
    }


def _apply_documents(*, batch: ImportBatch):
    model = FirePlan if batch.domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    decisions = _review_decisions(batch)
    existing = {
        _document_identity_key(row, domain=batch.domain): row
        for row in model.objects.select_for_update().filter(department=batch.department)
    }
    add = update = deactivate = unchanged = skipped = 0
    for row in batch.normalized_intent["rows"]:
        current = existing.get(_document_identity_key(row, domain=batch.domain))
        if row["action"] == "deactivate":
            if current is not None and current.active:
                current.active = False
                current.save(update_fields=("active", "updated_at"))
                deactivate += 1
            else:
                unchanged += 1
            continue
        if current is None:
            content_changed = False
        else:
            content_changed = _document_content_changed(current=current, row=row, model=model)
            metadata_changed = _document_metadata_changes(current=current, row=row, model=model)
            if content_changed or metadata_changed:
                # A proposed update is gated on an explicit review decision. Pending
                # blocks confirmation; skipped produces zero canonical/PDF/publication
                # effect; only approved updates mutate canonical rows.
                decision = decisions.get(str(current.id))
                if decision not in _REVIEW_DECISIONS:
                    raise ImportError(
                        "Pending update review requires approval or skip before import."
                    )
                if decision == "skipped":
                    skipped += 1
                    continue
        is_new_content = current is None or content_changed
        sanitized_path = None
        if is_new_content:
            sanitized_path = settings.INGESTION_STAGING_ROOT / row["sanitized_staging_key"]
            if (
                not sanitized_path.is_file()
                or hashlib.sha256(sanitized_path.read_bytes()).hexdigest()
                != row["sanitized_pdf_sha256"]
            ):
                raise ImportError("Sanitized preview output is unavailable; create a new preview.")
        if current is None:
            current = _create_document(
                batch=batch, row=row, model=model, sanitized_path=sanitized_path
            )
            add += 1
            if model is KlgvPlan:
                record_event(
                    action="reference_data.klgv_plan_created",
                    actor_user=batch.actor,
                    department=batch.department,
                    target_type="klgv_plan",
                    target_uuid=current.id,
                    metadata={"external_identifier": current.external_identifier or None},
                )
        elif is_new_content:
            _replace_document_content(
                current=current, row=row, model=model, sanitized_path=sanitized_path
            )
            update += 1
            if model is KlgvPlan:
                record_event(
                    action="reference_data.klgv_plan_updated",
                    actor_user=batch.actor,
                    department=batch.department,
                    target_type="klgv_plan",
                    target_uuid=current.id,
                    metadata={"external_identifier": current.external_identifier or None},
                )
        elif _merge_document_metadata(current=current, row=row, model=model):
            update += 1
            if model is KlgvPlan:
                record_event(
                    action="reference_data.klgv_plan_updated",
                    actor_user=batch.actor,
                    department=batch.department,
                    target_type="klgv_plan",
                    target_uuid=current.id,
                    metadata={"external_identifier": current.external_identifier or None},
                )
        else:
            unchanged += 1
    batch.validation_summary["skipped_update_count"] = skipped
    code = "department_fire_plans" if model is FirePlan else "department_klgv_plans"
    scopes = [(code, None)] if add or update or deactivate else []
    return scopes, (add, update, deactivate, unchanged)


def _create_document(*, batch, row, model, sanitized_path):
    document_id = uuid.uuid4()
    key = f"plans/{document_id}.pdf" if model is KlgvPlan else f"{document_id}.pdf"
    if model is KlgvPlan:
        (settings.REFERENCE_DATA_ACCEPTED_ROOT / "plans").mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
    promote_to_accepted(sanitized_path, key)
    common = {
        "id": document_id,
        "department": batch.department,
        "external_identifier": row["external_identifier"],
        "document_key": key,
        "original_filename": row["original_filename"],
        "file_size": row["file_size"],
        "page_count": row["page_count"],
        "active": True,
        "uploaded_by": batch.actor,
    }
    if model is FirePlan:
        return FirePlan.objects.create(
            **common,
            object_name=row["title"],
            address=row["address"],
            location=_location(row),
            postal_code=row["postal_code"],
            city=row["city"],
            fsd_location=row["fsd_location"],
            bmz_location=row["bmz_location"],
            rwa_info=row["rwa_info"],
            sha256=row["sanitized_pdf_sha256"],
            source_pdf_sha256=row["source_pdf_sha256"],
        )
    klgv_common = common | {"path": common["document_key"]}
    del klgv_common["document_key"]
    return KlgvPlan.objects.create(
        **klgv_common,
        object_name=row["title"],
        address=row["address"],
        postal_code=row["postal_code"],
        city=row["city"],
        location=_location(row),
        source_pdf_sha256=row["source_pdf_sha256"],
        sha256=row["sanitized_pdf_sha256"],
    )


def _replace_document_content(*, current, row, model, sanitized_path):
    key = f"plans/{current.id}.pdf" if model is KlgvPlan else f"{uuid.uuid4()}.pdf"
    if model is KlgvPlan:
        promote_to_accepted(sanitized_path, key, replace=True)
    else:
        promote_to_accepted(sanitized_path, key)
    if model is FirePlan:
        current.document_key = key
    current.original_filename = row["original_filename"]
    current.file_size = row["file_size"]
    current.page_count = row["page_count"]
    current.active = True
    if model is FirePlan:
        current.sha256 = row["sanitized_pdf_sha256"]
        current.source_pdf_sha256 = row["source_pdf_sha256"]
        for field, value in (
            ("object_name", row["title"]),
            ("address", row["address"]),
            ("postal_code", row["postal_code"]),
            ("city", row["city"]),
            ("fsd_location", row["fsd_location"]),
            ("bmz_location", row["bmz_location"]),
            ("rwa_info", row["rwa_info"]),
        ):
            if value:
                setattr(current, field, value)
        current.location = _location(row) or current.location
    else:
        current.path = key
        current.sha256 = row["sanitized_pdf_sha256"]
        current.source_pdf_sha256 = row["source_pdf_sha256"]
        current.object_name = row["title"]
        current.address = row["address"]
        current.postal_code = row["postal_code"]
        current.city = row["city"]
        current.location = _location(row)
    current.save()


def _merge_document_metadata(*, current, row, model) -> bool:
    changed = _document_metadata_changes(current=current, row=row, model=model)
    if changed:
        if model is FirePlan:
            for field, value in (
                ("object_name", row["title"]),
                ("address", row["address"]),
                ("postal_code", row["postal_code"]),
                ("city", row["city"]),
                ("fsd_location", row["fsd_location"]),
                ("bmz_location", row["bmz_location"]),
                ("rwa_info", row["rwa_info"]),
            ):
                if value:
                    setattr(current, field, value)
            if _location(row) is not None:
                current.location = _location(row)
        else:
            for field, value in (
                ("object_name", row["title"]),
                ("address", row["address"]),
                ("postal_code", row["postal_code"]),
                ("city", row["city"]),
            ):
                setattr(current, field, value)
            current.location = _location(row)
        current.save()
    return changed


def _document_content_changed(*, current, row, model) -> bool:
    return bool(current.source_pdf_sha256 != row["source_pdf_sha256"])


def _document_changed_fields(*, current, row, model) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    if _document_content_changed(current=current, row=row, model=model):
        sanitized_current = current.sha256
        fields.extend(
            (
                {
                    "name": "source_pdf_sha256",
                    "label": "Source PDF SHA-256",
                    "current": current.source_pdf_sha256 or "",
                    "proposed": row["source_pdf_sha256"],
                },
                {
                    "name": "sanitized_pdf_sha256",
                    "label": "Sanitized PDF SHA-256",
                    "current": sanitized_current or "",
                    "proposed": row["sanitized_pdf_sha256"],
                },
                {
                    "name": "pdf_size",
                    "label": "PDF size",
                    "current": _mib_format(current.file_size),
                    "proposed": _mib_format(row["file_size"]),
                },
                {
                    "name": "page_count",
                    "label": "Pages",
                    "current": current.page_count,
                    "proposed": row["page_count"],
                },
            )
        )
    if model is FirePlan:
        for name, label, attribute, proposed in (
            ("object_name", "Object name", "object_name", row["title"]),
            ("address", "Address", "address", row["address"]),
            ("postal_code", "Postal code", "postal_code", row["postal_code"]),
            ("city", "City", "city", row["city"]),
            ("fsd_location", "FSD location", "fsd_location", row["fsd_location"]),
            ("bmz_location", "BMZ location", "bmz_location", row["bmz_location"]),
            ("rwa_info", "RWA information", "rwa_info", row["rwa_info"]),
        ):
            if proposed and getattr(current, attribute) != proposed:
                fields.append(
                    {
                        "name": name,
                        "label": label,
                        "current": getattr(current, attribute) or "",
                        "proposed": proposed,
                    }
                )
            incoming_location = _location(row)
            if incoming_location is not None and current.location != incoming_location:
                field: dict[str, object] = {
                    "name": "location",
                    "label": "Location",
                    "current": (
                        {"longitude": current.location.x, "latitude": current.location.y}
                        if current.location is not None
                        else None
                    ),
                    "proposed": {
                        "longitude": incoming_location.x,
                        "latitude": incoming_location.y,
                    },
                }
                if current.location is not None:
                    field["distance_km"] = round(
                        _haversine_km(
                            current.location.y,
                            current.location.x,
                            incoming_location.y,
                            incoming_location.x,
                        ),
                        3,
                    )
                fields.append(field)
    else:
        for name, label, proposed in (
            ("object_name", "Object name", row["title"]),
            ("address", "Address", row["address"]),
            ("postal_code", "Postal code", row["postal_code"]),
            ("city", "City", row["city"]),
        ):
            if proposed and getattr(current, name) != proposed:
                fields.append(
                    {
                        "name": name,
                        "label": label,
                        "current": getattr(current, name),
                        "proposed": proposed,
                    }
                )
    return fields


def _document_metadata_changes(*, current, row, model) -> bool:
    changed = False
    if model is FirePlan:
        for field, value in (
            ("object_name", row["title"]),
            ("address", row["address"]),
            ("postal_code", row["postal_code"]),
            ("city", row["city"]),
            ("fsd_location", row["fsd_location"]),
            ("bmz_location", row["bmz_location"]),
            ("rwa_info", row["rwa_info"]),
        ):
            if value and getattr(current, field) != value:
                changed = True
        if _location(row) is not None and current.location != _location(row):
            changed = True
    else:
        for field, value in (
            ("object_name", row["title"]),
            ("address", row["address"]),
            ("postal_code", row["postal_code"]),
            ("city", row["city"]),
        ):
            if getattr(current, field) != value:
                changed = True
        if _location(row) != current.location:
            changed = True
    return changed


def _location(row: dict[str, object]) -> Point | None:
    if row.get("longitude") is None or row.get("latitude") is None:
        return None
    longitude = cast(str | int | float, row["longitude"])
    latitude = cast(str | int | float, row["latitude"])
    return Point(float(longitude), float(latitude), srid=4326)
