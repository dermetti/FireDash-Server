from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.publications.services import mark_dirty
from apps.reference_data.hydrants import NormalizedHydrant, parse_feature_collection
from apps.reference_data.models import FirePlan, Hydrant, HydrantImportPreview, KlgvPlan


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
    hydrant = Hydrant.objects.select_for_update().select_related("department").get(pk=hydrant.pk)
    require_department_admin(actor, hydrant.department)
    previous_active = hydrant.active
    for field in ("external_identifier", "hydrant_type", "status"):
        if field in values:
            setattr(hydrant, field, str(values[field]).strip())
    if "flow_information" in values:
        hydrant.flow_information = str(values["flow_information"]).strip()
    if "diameter_mm" in values:
        hydrant.diameter_mm = values["diameter_mm"] or None
    if "longitude" in values and "latitude" in values:
        hydrant.location = Point(float(values["longitude"]), float(values["latitude"]), srid=4326)
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


@transaction.atomic
def delete_hydrant(*, actor, hydrant: Hydrant) -> None:
    """Permanently remove an erroneous hydrant without deleting dependencies."""
    hydrant = Hydrant.objects.select_for_update().select_related("department").get(pk=hydrant.pk)
    require_department_admin(actor, hydrant.department)
    hydrant_id = hydrant.id
    department = hydrant.department
    metadata: dict[str, str | int | bool | None] = {
        "external_identifier": hydrant.external_identifier or None,
        "longitude": str(hydrant.location.x),
        "latitude": str(hydrant.location.y),
    }
    try:
        hydrant.delete()
    except ProtectedError as error:
        raise ValueError("Hydrant cannot be deleted while protected records exist.") from error
    record_event(
        action="reference_data.hydrant_deleted",
        actor_user=actor,
        department=department,
        target_type="hydrant",
        target_uuid=hydrant_id,
        metadata=metadata,
    )
    mark_dirty(department=department, dataset_type_code="department_hydrants", actor=actor)


@transaction.atomic
def set_fire_plan_active(*, actor, fire_plan, active: bool):
    require_department_admin(actor, fire_plan.department)
    previous_active = fire_plan.active
    if previous_active == active:
        return fire_plan
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


@transaction.atomic
def update_fire_plan(*, actor, fire_plan: FirePlan, **values) -> FirePlan:
    fire_plan = (
        FirePlan.objects.select_for_update().select_related("department").get(pk=fire_plan.pk)
    )
    require_department_admin(actor, fire_plan.department)
    for field in (
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "fsd_location",
        "bmz_location",
        "rwa_info",
    ):
        if field in values:
            setattr(fire_plan, field, str(values[field] or "").strip())
    if "longitude" in values and "latitude" in values:
        longitude, latitude = values["longitude"], values["latitude"]
        fire_plan.location = (
            Point(float(longitude), float(latitude), srid=4326)
            if longitude is not None and latitude is not None
            else None
        )
    fire_plan.full_clean()
    fire_plan.save()
    record_event(
        action="reference_data.fire_plan_updated",
        actor_user=actor,
        department=fire_plan.department,
        target_type="fire_plan",
        target_uuid=fire_plan.id,
        metadata={"external_identifier": fire_plan.external_identifier or None},
    )
    mark_dirty(
        department=fire_plan.department, dataset_type_code="department_fire_plans", actor=actor
    )
    return fire_plan


@transaction.atomic
def delete_fire_plan(*, actor, fire_plan: FirePlan) -> None:
    """Delete an erroneous, exclusively-owned accepted document after commit."""
    fire_plan = (
        FirePlan.objects.select_for_update().select_related("department").get(pk=fire_plan.pk)
    )
    require_department_admin(actor, fire_plan.department)
    plan_id, department, document_key = fire_plan.id, fire_plan.department, fire_plan.document_key
    metadata: dict[str, str | int | bool | None] = {
        "external_identifier": fire_plan.external_identifier or None,
        "address": fire_plan.address or None,
        "object_name": fire_plan.object_name or None,
    }
    try:
        fire_plan.delete()
    except ProtectedError as error:
        raise ValueError("Fire Plan cannot be deleted while protected records exist.") from error
    record_event(
        action="reference_data.fire_plan_deleted",
        actor_user=actor,
        department=department,
        target_type="fire_plan",
        target_uuid=plan_id,
        metadata=metadata,
    )
    mark_dirty(department=department, dataset_type_code="department_fire_plans", actor=actor)
    accepted_path = settings.REFERENCE_DATA_ACCEPTED_ROOT / Path(document_key).name
    transaction.on_commit(lambda: accepted_path.unlink(missing_ok=True))


@transaction.atomic
def set_klgv_plan_active(*, actor, klgv_plan: KlgvPlan, active: bool) -> KlgvPlan:
    """Explicit lifecycle action; an import omission never reaches this path."""
    require_department_admin(actor, klgv_plan.department)
    if klgv_plan.active == active:
        return klgv_plan
    klgv_plan.active = active
    klgv_plan.save(update_fields=("active", "updated_at"))
    record_event(
        action=(
            "reference_data.klgv_plan_activated"
            if active
            else "reference_data.klgv_plan_deactivated"
        ),
        actor_user=actor,
        department=klgv_plan.department,
        target_type="klgv_plan",
        target_uuid=klgv_plan.id,
        metadata={"active": active},
    )
    mark_dirty(
        department=klgv_plan.department,
        dataset_type_code="department_klgv_plans",
        actor=actor,
    )
    return klgv_plan


@transaction.atomic
def update_klgv_plan(*, actor, klgv_plan: KlgvPlan, **values) -> KlgvPlan:
    klgv_plan = (
        KlgvPlan.objects.select_for_update().select_related("department").get(pk=klgv_plan.pk)
    )
    require_department_admin(actor, klgv_plan.department)
    for field in ("external_identifier", "title", "category"):
        if field in values:
            setattr(klgv_plan, field, str(values[field] or "").strip())
    klgv_plan.full_clean()
    klgv_plan.save()
    record_event(
        action="reference_data.klgv_plan_updated",
        actor_user=actor,
        department=klgv_plan.department,
        target_type="klgv_plan",
        target_uuid=klgv_plan.id,
        metadata={"external_identifier": klgv_plan.external_identifier, "title": klgv_plan.title},
    )
    mark_dirty(
        department=klgv_plan.department, dataset_type_code="department_klgv_plans", actor=actor
    )
    return klgv_plan


@transaction.atomic
def delete_klgv_plan(*, actor, klgv_plan: KlgvPlan) -> None:
    klgv_plan = (
        KlgvPlan.objects.select_for_update().select_related("department").get(pk=klgv_plan.pk)
    )
    require_department_admin(actor, klgv_plan.department)
    plan_id, department, document_key = klgv_plan.id, klgv_plan.department, klgv_plan.document_key
    metadata: dict[str, str | int | bool | None] = {
        "external_identifier": klgv_plan.external_identifier,
        "title": klgv_plan.title,
    }
    try:
        klgv_plan.delete()
    except ProtectedError as error:
        raise ValueError("KLGV plan cannot be deleted while protected records exist.") from error
    record_event(
        action="reference_data.klgv_plan_deleted",
        actor_user=actor,
        department=department,
        target_type="klgv_plan",
        target_uuid=plan_id,
        metadata=metadata,
    )
    mark_dirty(department=department, dataset_type_code="department_klgv_plans", actor=actor)
    accepted_path = settings.REFERENCE_DATA_ACCEPTED_ROOT / Path(document_key).name
    transaction.on_commit(lambda: accepted_path.unlink(missing_ok=True))


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
