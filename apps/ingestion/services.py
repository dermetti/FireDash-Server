"""Canonical ingestion orchestration.

No function in this module calls a publication builder.  It commits canonical
rows first, then marks each unique publication scope dirty once in the same
transaction.  The worker pipeline remains the only artifact producer.
"""

import csv
import hashlib
import io
import json
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
from apps.assignments.services import ensure_current_home
from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.ingestion.models import ImportBatch
from apps.ingestion.parsers import ImportValidationError, parse_hydrants, parse_personnel
from apps.ingestion.pdf_packages import parse_pdf_package
from apps.ingestion.storage import ImportStorageError, read_staged, remove_staged, stage_upload
from apps.organizations.models import Station
from apps.personnel.models import Person
from apps.publications.services import mark_dirty
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan
from apps.reference_data.pdf_sandbox import PdfSanitizerError, sanitize
from apps.reference_data.pdf_validation import PdfValidationError, validate_pdf
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
            ("personnel_number", "first_name", "last_name", "incident_commander_eligible"), values
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
    else:
        fields = ("external_id", "filename", "title", "category", "action")
        row = {
            "external_id": values.get("external_id", ""),
            "filename": "document.pdf",
            "title": values.get("title", ""),
            "category": values.get("category", ""),
            "action": "upsert",
        }
    manifest = _csv_payload(fields, row)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", manifest)
        archive.writestr("document.pdf", pdf_bytes)
    return output.getvalue()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _hydrant_baseline(*, department) -> dict[str, str]:
    return {
        identifier: _fingerprint(
            {
                "updated_at": hydrant.updated_at.isoformat(),
                "business_values": _hydrant_business_values(hydrant),
            }
        )
        for hydrant in Hydrant.objects.filter(
            department=department, external_identifier__gt=""
        ).only("external_identifier", "updated_at", "status", "location")
        for identifier in [hydrant.external_identifier]
    }


def _hydrant_business_values(hydrant_or_row) -> dict[str, object]:
    """The import's canonical business representation, never persistence metadata."""
    if isinstance(hydrant_or_row, Hydrant):
        return {
            "longitude": hydrant_or_row.location.x,
            "latitude": hydrant_or_row.location.y,
            "hydrant_type": hydrant_or_row.hydrant_type,
            "diameter_mm": hydrant_or_row.diameter_mm,
            "status": hydrant_or_row.status,
        }
    return {
        field: hydrant_or_row[field]
        for field in ("longitude", "latitude", "hydrant_type", "diameter_mm", "status")
    }


def _hydrant_changed_fields(*, current: Hydrant, proposed: dict[str, object]) -> list[str]:
    current_values = _hydrant_business_values(current)
    proposed_values = _hydrant_business_values(proposed)
    return [field for field in proposed_values if current_values[field] != proposed_values[field]]


def _personnel_baseline(*, department) -> dict[str, str]:
    return {
        number: _fingerprint({"updated_at": person.updated_at.isoformat(), "active": person.active})
        for person in Person.objects.filter(
            department=department,
            personnel_number__isnull=False,
            lifecycle_status=Person.LifecycleStatus.ACTIVE,
        ).only("personnel_number", "updated_at", "active")
        for number in [person.personnel_number or ""]
    }


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
    try:
        if domain == ImportBatch.Domain.HYDRANTS:
            if import_mode != ImportBatch.Mode.MERGE:
                raise ImportError("Hydrant imports require merge mode.")
            intent = parse_hydrants(payload=payload, import_format=import_format)
            baseline = _hydrant_baseline(department=department)
            counts, hydrant_updates, updates_truncated = _preview_hydrants(
                intent=intent, department=department
            )
        elif domain == ImportBatch.Domain.PERSONNEL:
            if import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("Personnel imports support upsert mode only.")
            intent = parse_personnel(payload=payload, import_format=import_format)
            baseline = _personnel_baseline(department=department)
            counts, updates, updates_truncated = _preview_personnel(
                intent=intent, department=department
            )
        elif domain in {ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS}:
            if import_format != ImportBatch.Format.ZIP or import_mode != ImportBatch.Mode.UPSERT:
                raise ImportError("PDF package imports require ZIP upsert mode.")
            intent = _sanitize_pdf_preview(
                batch=batch, payload=payload, department=department, domain=domain
            )
            baseline = _document_baseline(department=department, domain=domain)
            counts, updates, updates_truncated = _preview_documents(
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
    }:
        batch.validation_summary["updates"] = updates
    batch.validation_summary["updates_truncated"] = updates_truncated
    if domain == ImportBatch.Domain.FIRE_PLANS:
        batch.validation_summary["coordinate_conflicts"] = _coordinate_conflicts(
            department=department, intent=intent
        )
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
        },
    )
    return batch


def _preview_hydrants(*, intent, department):
    existing = {
        hydrant.external_identifier: hydrant
        for hydrant in Hydrant.objects.filter(department=department, external_identifier__gt="")
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
            if _hydrant_baseline(department=batch.department) != batch.baseline:
                raise ImportError("Canonical hydrants changed; re-preview is required.")
            scopes, counts = _apply_hydrants(batch=batch, rows=rows)
        elif batch.domain == ImportBatch.Domain.PERSONNEL:
            rows = parse_personnel(payload=payload, import_format=batch.import_format)
            if _personnel_baseline(department=batch.department) != batch.baseline:
                raise ImportError("Canonical personnel changed; re-preview is required.")
            scopes, counts = _apply_personnel(batch=batch, rows=rows)
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
        },
    )
    return batch


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
    existing = {
        hydrant.external_identifier: hydrant
        for hydrant in Hydrant.objects.select_for_update().filter(
            department=batch.department, external_identifier__gt=""
        )
    }
    add = update = unchanged = 0
    to_create: list[Hydrant] = []
    to_update: list[Hydrant] = []
    now = timezone.now()
    for row in rows:
        identifier = row["external_identifier"]
        values = {
            "location": Point(row["longitude"], row["latitude"], srid=4326),
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
            fields=("location", "hydrant_type", "diameter_mm", "status", "updated_at"),
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
    add = update = unchanged = 0
    changed_people: list[Person] = []
    for row in rows:
        person = existing.get(row["personnel_number"])
        display_name = f"{row['first_name']} {row['last_name']}".strip()
        if person is None:
            if batch.station is None:
                raise ImportError("New personnel require an explicit home station.")
            person = Person.objects.create(
                department=batch.department, display_name=display_name, **row
            )
            PersonnelStationAssignment.objects.create(
                person=person,
                station=batch.station,
                assignment_type=PersonnelStationAssignment.AssignmentType.HOME,
                valid_from=timezone.now(),
                created_by=batch.actor,
            )
            ensure_current_home(person)
            add += 1
            changed_people.append(person)
        elif (
            _personnel_changed(current=person, proposed=row) or person.display_name != display_name
        ):
            for field, value in row.items():
                setattr(person, field, value)
            person.display_name = display_name
            person.save()
            update += 1
            changed_people.append(person)
        else:
            unchanged += 1
    station_ids = set(
        Station.objects.filter(
            personnel_assignments__person__in=changed_people,
            personnel_assignments__ended_at__isnull=True,
        ).values_list("id", flat=True)
    )
    scopes = [
        ("station_personnel", station)
        for station in Station.objects.filter(id__in=station_ids).order_by("id")
    ]
    return scopes, (add, update, 0, unchanged)


def _document_identity_key(document_or_row, *, domain: str) -> str:
    """Return the canonical import identity, never storage/persistence metadata."""
    external_identifier = str(
        document_or_row["external_identifier"]
        if isinstance(document_or_row, dict)
        else document_or_row.external_identifier
    ).strip()
    if domain != ImportBatch.Domain.FIRE_PLANS:
        return f"external_identifier:{external_identifier}"
    address = str(
        document_or_row["address"] if isinstance(document_or_row, dict) else document_or_row.address
    ).strip()
    if external_identifier:
        return f"external_identifier:{external_identifier}"
    return f"address:{address}"


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
                }
            )
            for row in rows
        }
    klgv_rows = KlgvPlan.objects.filter(department=department).only(
        "external_identifier",
        "updated_at",
        "active",
        "sanitized_pdf_sha256",
        "title",
        "category",
    )
    return {
        _document_identity_key(row, domain=domain): _fingerprint(
            {
                "updated_at": row.updated_at.isoformat(),
                "active": row.active,
                "sha256": row.sanitized_pdf_sha256,
                "title": row.title,
                "category": row.category,
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
            if len(details) < settings.MAX_IMPORT_VALIDATION_ERRORS:
                fields = _document_changed_fields(current=current, row=row, model=model)
                details.append(
                    {"external_identifier": row["external_identifier"], "fields": fields}
                )
        else:
            unchanged += 1
    return (add, update, deactivate, unchanged), details, update > len(details)


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
) -> list[dict[str, object]]:
    """Use the production quarantine/broker path, but keep output private/staged.

    This deliberately does not promote anything into canonical accepted storage.
    """
    entries = parse_pdf_package(payload=payload, domain=domain)
    model = FirePlan if domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    existing = {
        _document_identity_key(row, domain=domain): row
        for row in model.objects.filter(department=department)
    }
    result: list[dict[str, object]] = []
    try:
        for entry in entries:
            metadata: dict[str, object] = {
                "external_identifier": entry.external_identifier,
                "title": entry.title,
                "address": entry.address,
                "postal_code": entry.postal_code,
                "city": entry.city,
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
                            current.sha256
                            if isinstance(current, FirePlan)
                            else current.sanitized_pdf_sha256
                        ),
                        "file_size": current.file_size,
                        "page_count": current.page_count,
                        "content_reused": True,
                    }
                )
                continue
            quarantine = sanitized = None
            try:
                uploaded = SimpleUploadedFile(
                    entry.filename, entry.pdf_bytes or b"", content_type="application/pdf"
                )
                quarantine = write_quarantine(uploaded)
                validate_pdf(quarantine, original_filename=entry.filename)
                sanitized = output_path(job_id=quarantine.parent.name)
                sanitize(quarantined_input=quarantine, sanitized_output=sanitized)
                file_size, page_count, sanitized_sha256 = validate_pdf(sanitized)
                output_key = f"{batch.id}.{uuid.uuid4()}.sanitized.pdf"
                target = settings.INGESTION_STAGING_ROOT / output_key
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(sanitized, target)
                os.chmod(target, 0o600)
            except (
                PdfSanitizerError,
                PdfValidationError,
                ReferenceDataStorageError,
                OSError,
            ) as error:
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
    return result


def _apply_documents(*, batch: ImportBatch):
    model = FirePlan if batch.domain == ImportBatch.Domain.FIRE_PLANS else KlgvPlan
    existing = {
        _document_identity_key(row, domain=batch.domain): row
        for row in model.objects.select_for_update().filter(department=batch.department)
    }
    add = update = deactivate = unchanged = 0
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
        is_new_content = current is None or _document_content_changed(
            current=current, row=row, model=model
        )
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
        elif is_new_content:
            _replace_document_content(
                current=current, row=row, model=model, sanitized_path=sanitized_path
            )
            update += 1
        elif _merge_document_metadata(current=current, row=row, model=model):
            update += 1
        else:
            unchanged += 1
    code = "department_fire_plans" if model is FirePlan else "department_klgv_plans"
    scopes = [(code, None)] if add or update or deactivate else []
    return scopes, (add, update, deactivate, unchanged)


def _create_document(*, batch, row, model, sanitized_path):
    document_id = uuid.uuid4()
    key = f"{document_id}.pdf"
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
            sha256=row["sanitized_pdf_sha256"],
            source_pdf_sha256=row["source_pdf_sha256"],
        )
    return KlgvPlan.objects.create(
        **common,
        title=row["title"],
        category=row["category"],
        source_pdf_sha256=row["source_pdf_sha256"],
        sanitized_pdf_sha256=row["sanitized_pdf_sha256"],
    )


def _replace_document_content(*, current, row, model, sanitized_path):
    key = f"{uuid.uuid4()}.pdf"
    promote_to_accepted(sanitized_path, key)
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
        ):
            if value:
                setattr(current, field, value)
        current.location = _location(row) or current.location
    else:
        current.sanitized_pdf_sha256 = row["sanitized_pdf_sha256"]
        current.source_pdf_sha256 = row["source_pdf_sha256"]
        current.title = row["title"]
        current.category = row["category"] or current.category
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
            ):
                if value:
                    setattr(current, field, value)
            if _location(row) is not None:
                current.location = _location(row)
        else:
            for field, value in (("title", row["title"]), ("category", row["category"])):
                if value:
                    setattr(current, field, value)
        current.save()
    return changed


def _document_content_changed(*, current, row, model) -> bool:
    return bool(current.source_pdf_sha256 != row["source_pdf_sha256"])


def _document_changed_fields(*, current, row, model) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    if _document_content_changed(current=current, row=row, model=model):
        fields.append(
            {"name": "source_pdf", "current": "existing PDF", "proposed": "replacement PDF"}
        )
    if model is FirePlan:
        pairs = (
            ("title", "object_name", row["title"]),
            ("address", "address", row["address"]),
            ("postal_code", "postal_code", row["postal_code"]),
            ("city", "city", row["city"]),
        )
        for label, attribute, proposed in pairs:
            if proposed and getattr(current, attribute) != proposed:
                fields.append(
                    {"name": label, "current": getattr(current, attribute), "proposed": proposed}
                )
        if _location(row) is not None and current.location != _location(row):
            fields.append(
                {
                    "name": "location",
                    "current": "existing location",
                    "proposed": "replacement location",
                }
            )
    else:
        for label, proposed in (("title", row["title"]), ("category", row["category"])):
            if proposed and getattr(current, label) != proposed:
                fields.append(
                    {"name": label, "current": getattr(current, label), "proposed": proposed}
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
        ):
            if value and getattr(current, field) != value:
                changed = True
        if _location(row) is not None and current.location != _location(row):
            changed = True
    else:
        for field, value in (("title", row["title"]), ("category", row["category"])):
            if value and getattr(current, field) != value:
                changed = True
    return changed


def _location(row: dict[str, object]) -> Point | None:
    longitude = cast(str | int | float, row["longitude"])
    latitude = cast(str | int | float, row["latitude"])
    return (
        Point(float(longitude), float(latitude), srid=4326)
        if row.get("longitude") is not None
        else None
    )
