import uuid
from datetime import timedelta

from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.publications.services import mark_dirty
from apps.reference_data.hydrants import NormalizedHydrant, parse_feature_collection
from apps.reference_data.models import FirePlan, Hydrant, HydrantImportPreview
from apps.reference_data.pdf_sandbox import PdfSanitizerError, sanitize
from apps.reference_data.pdf_validation import PdfValidationError, validate_pdf
from apps.reference_data.storage import (
    StorageError,
    cleanup,
    output_path,
    promote_to_accepted,
    write_quarantine,
)


class ReferenceDataError(ValueError):
    pass


@transaction.atomic
def create_hydrant(
    *,
    actor,
    department,
    longitude: float,
    latitude: float,
    external_identifier: str = "",
    hydrant_type: str = "",
    diameter_mm: int | None = None,
    status: str = "ACTIVE",
) -> Hydrant:
    require_department_admin(actor, department)
    hydrant = Hydrant.objects.create(
        department=department,
        location=Point(longitude, latitude, srid=4326),
        external_identifier=external_identifier.strip(),
        hydrant_type=hydrant_type.strip(),
        diameter_mm=diameter_mm,
        status=status,
        source_metadata={},
    )
    record_event(
        action="reference_data.hydrant_created",
        actor_user=actor,
        department=department,
        target_type="hydrant",
        target_uuid=hydrant.id,
    )
    mark_dirty(department=department, dataset_type_code="department_hydrants", actor=actor)
    return hydrant


def create_hydrant_preview(*, actor, department, raw_geojson: bytes) -> HydrantImportPreview:
    require_department_admin(actor, department)
    features = parse_feature_collection(raw_geojson)
    duplicates = _probable_duplicate_count(department=department, features=features)
    return HydrantImportPreview.objects.create(
        department=department,
        created_by=actor,
        normalized_features=[feature.as_json() for feature in features],
        duplicate_count=duplicates,
        expires_at=timezone.now() + timedelta(minutes=15),
    )


@transaction.atomic
def confirm_hydrant_preview(*, actor, department, preview_id) -> tuple[int, int, int]:
    require_department_admin(actor, department)
    preview = HydrantImportPreview.objects.select_for_update().filter(pk=preview_id).first()
    if (
        preview is None
        or preview.department_id != department.id
        or preview.created_by_id != actor.id
        or preview.expires_at <= timezone.now()
    ):
        raise PermissionDenied("Hydrant import preview is unavailable.")
    features = [NormalizedHydrant(**feature) for feature in preview.normalized_features]
    created = updated = 0
    for feature in features:
        values = {
            "location": Point(feature.longitude, feature.latitude, srid=4326),
            "hydrant_type": feature.hydrant_type,
            "diameter_mm": feature.diameter_mm,
            "status": feature.status
            if feature.status in {"ACTIVE", "INACTIVE", "UNKNOWN"}
            else "ACTIVE",
            "source_metadata": feature.source_metadata,
        }
        if feature.external_identifier:
            hydrant, was_created = Hydrant.objects.update_or_create(
                department=department,
                external_identifier=feature.external_identifier,
                defaults=values,
            )
        else:
            Hydrant.objects.create(department=department, external_identifier="", **values)
            was_created = True
        if was_created:
            created += 1
        else:
            updated += 1
    duplicate_count = preview.duplicate_count
    preview.delete()
    record_event(
        action="reference_data.hydrants_imported",
        actor_user=actor,
        department=department,
        target_type="hydrant_import",
        metadata={"created": created, "updated": updated, "duplicates": duplicate_count},
    )
    mark_dirty(department=department, dataset_type_code="department_hydrants", actor=actor)
    return created, updated, duplicate_count


@transaction.atomic
def update_hydrant(*, actor, hydrant: Hydrant, **values) -> Hydrant:
    require_department_admin(actor, hydrant.department)
    previous_active = hydrant.active
    for field in ("external_identifier", "hydrant_type", "status"):
        if field in values:
            setattr(hydrant, field, str(values[field]).strip())
    if "diameter_mm" in values:
        hydrant.diameter_mm = values["diameter_mm"] or None
    hydrant.save()
    new_active = hydrant.active
    record_event(
        action=(
            "reference_data.hydrant_activated"
            if new_active and not previous_active
            else "reference_data.hydrant_deactivated"
            if not new_active and previous_active
            else "reference_data.hydrant_updated"
        ),
        actor_user=actor,
        department=hydrant.department,
        target_type="hydrant",
        target_uuid=hydrant.id,
        metadata={"status": hydrant.status, "diameter_mm": hydrant.diameter_mm},
    )
    mark_dirty(department=hydrant.department, dataset_type_code="department_hydrants", actor=actor)
    return hydrant


def accept_fire_plan(
    *,
    actor,
    department,
    uploaded_file,
    object_name: str,
    object_reference: str = "",
    address: str = "",
    location: Point | None = None,
):
    """Accept only a revalidated, sandbox-sanitized PDF into private storage."""
    require_department_admin(actor, department)
    quarantine = sanitized = accepted = None
    try:
        quarantine = write_quarantine(uploaded_file)
        validate_pdf(quarantine, original_filename=uploaded_file.name)
        sanitized = output_path()
        sanitize(quarantined_input=quarantine, sanitized_output=sanitized)
        file_size, page_count, digest = validate_pdf(sanitized)
        plan_id = uuid.uuid4()
        document_key = f"{plan_id}.pdf"
        accepted = promote_to_accepted(sanitized, document_key)
        with transaction.atomic():
            fire_plan = FirePlan.objects.create(
                id=plan_id,
                department=department,
                object_name=object_name.strip(),
                object_reference=object_reference.strip(),
                address=address.strip(),
                location=location,
                document_key=document_key,
                original_filename=uploaded_file.name[:255],
                file_size=file_size,
                page_count=page_count,
                sha256=digest,
                uploaded_by=actor,
            )
            record_event(
                action="reference_data.fire_plan_accepted",
                actor_user=actor,
                department=department,
                target_type="fire_plan",
                target_uuid=fire_plan.id,
                metadata={"file_size": file_size, "page_count": page_count, "sha256": digest},
            )
            mark_dirty(
                department=department, dataset_type_code="department_fire_plans", actor=actor
            )
        return fire_plan
    except (StorageError, PdfValidationError, PdfSanitizerError) as error:
        cleanup(accepted)
        record_event(
            action=(
                "reference_data.pdf_sanitizer_failed"
                if isinstance(error, PdfSanitizerError)
                else "reference_data.pdf_rejected"
            ),
            actor_user=actor,
            department=department,
            target_type="fire_plan_upload",
            metadata={"error_code": getattr(error, "code", "validation_failed")},
        )
        raise ReferenceDataError("Fire plan was rejected.") from error
    finally:
        cleanup(quarantine)
        cleanup(sanitized)


@transaction.atomic
def set_fire_plan_active(*, actor, fire_plan, active: bool):
    require_department_admin(actor, fire_plan.department)
    previous_active = fire_plan.active
    fire_plan.active = active
    fire_plan.save(update_fields=("active", "updated_at"))
    record_event(
        action=(
            "reference_data.fire_plan_activated"
            if active and not previous_active
            else "reference_data.fire_plan_deactivated"
            if not active and previous_active
            else "reference_data.fire_plan_updated"
        ),
        actor_user=actor,
        department=fire_plan.department,
        target_type="fire_plan",
        target_uuid=fire_plan.id,
        metadata={"active": active},
    )
    mark_dirty(
        department=fire_plan.department, dataset_type_code="department_fire_plans", actor=actor
    )
    return fire_plan


def _probable_duplicate_count(*, department, features: list[NormalizedHydrant]) -> int:
    count = 0
    known = set(
        Hydrant.objects.filter(department=department).values_list(
            "location", "hydrant_type", "status"
        )
    )
    seen: set[tuple[float, float, str, str]] = set()
    for feature in features:
        key = (feature.longitude, feature.latitude, feature.hydrant_type, feature.status)
        if key in seen or any(
            location.x == feature.longitude
            and location.y == feature.latitude
            and hydrant_type == feature.hydrant_type
            and status == feature.status
            for location, hydrant_type, status in known
        ):
            count += 1
        seen.add(key)
    return count
