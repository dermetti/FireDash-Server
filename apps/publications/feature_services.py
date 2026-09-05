from django.db import IntegrityError, transaction

from apps.audit.services import record_event
from apps.publications.features import FeatureRegistryError, get_feature_definition
from apps.publications.models import DepartmentFeature


class FeatureDisabledError(ValueError):
    pass


def is_feature_enabled(*, department, feature_code: str) -> bool:
    try:
        get_feature_definition(feature_code)
    except FeatureRegistryError as error:
        raise FeatureDisabledError(str(error)) from error
    feature = (
        DepartmentFeature.objects.filter(department=department, feature_code=feature_code)
        .only("enabled")
        .first()
    )
    # Tablet-deliverable capabilities are enabled unless an administrator has
    # explicitly persisted a future deactivation decision.
    return True if feature is None else feature.enabled


def require_feature(*, department, feature_code: str) -> None:
    if not is_feature_enabled(department=department, feature_code=feature_code):
        raise FeatureDisabledError("This department has not enabled the required feature.")


@transaction.atomic
def set_department_feature(
    *, actor, department, feature_code: str, enabled: bool
) -> DepartmentFeature:
    try:
        get_feature_definition(feature_code)
    except FeatureRegistryError as error:
        raise FeatureDisabledError(str(error)) from error
    feature = (
        DepartmentFeature.objects.select_for_update()
        .filter(department=department, feature_code=feature_code)
        .first()
    )
    if feature is None:
        try:
            # Preserve the outer transaction when concurrent first enables race.
            with transaction.atomic():
                feature = DepartmentFeature.objects.create(
                    department=department, feature_code=feature_code, enabled=enabled
                )
        except IntegrityError:
            feature = DepartmentFeature.objects.select_for_update().get(
                department=department, feature_code=feature_code
            )
    if feature.enabled != enabled:
        feature.enabled = enabled
        feature.save(update_fields=("enabled", "updated_at"))
    record_event(
        action="publication.feature_updated",
        actor_user=actor,
        department=department,
        target_type="department_feature",
        target_uuid=None,
        metadata={"feature_code": feature_code, "enabled": enabled},
    )
    return feature
