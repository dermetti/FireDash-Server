from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class DatasetRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetTypeDefinition:
    code: str
    display_name: str
    scope: str
    artifact_format: str
    current_schema_version: int
    encryption_required: bool
    minimum_app_version: str | None
    builder_service: str
    validator_service: str
    summary_schema: Mapping[str, str]
    required: bool
    supported_schema_versions: tuple[int, ...]
    minimum_supported_schema_version: int
    maximum_supported_schema_version: int
    feature_code: str
    internal_only: bool = False


_DEFINITIONS = (
    DatasetTypeDefinition(
        code="department_hydrants",
        display_name="Department hydrants",
        scope="department",
        artifact_format="geojson",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="department_hydrants",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "active_count": "item_count",
                "source_revision": "non_negative_integer",
                "status_counts": "bounded_string_integer_map",
            }
        ),
        required=True,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=False,
    ),
    DatasetTypeDefinition(
        code="department_fire_plans",
        display_name="Department fire plans",
        scope="department",
        artifact_format="zip",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="department_fire_plans",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "active_document_count": "item_count",
                "total_accepted_bytes": "non_negative_integer",
                "total_pages": "non_negative_integer",
                "source_revision": "non_negative_integer",
            }
        ),
        required=True,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=False,
    ),
    DatasetTypeDefinition(
        code="station_personnel",
        display_name="Station personnel",
        scope="station",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="station_personnel",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "person_count": "item_count",
                "station_id": "uuid",
                "commander_eligible_count": "item_count",
                "verified_commander_email_count": "item_count",
                "source_revision": "non_negative_integer",
            }
        ),
        required=True,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=False,
    ),
    # This deliberately has no production source data. It proves that a new
    # department-scoped type is carried by the registry projection, not SQL checks.
    DatasetTypeDefinition(
        code="test_department_incidents",
        display_name="Test department incidents",
        scope="department",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="test_department_incidents",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "incident_count": "item_count",
                "source_revision": "non_negative_integer",
            }
        ),
        required=False,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=True,
    ),
)

DATASET_REGISTRY = MappingProxyType({definition.code: definition for definition in _DEFINITIONS})


def production_dataset_definitions() -> tuple[DatasetTypeDefinition, ...]:
    return tuple(definition for definition in _DEFINITIONS if not definition.internal_only)


def get_dataset_definition(code: str) -> DatasetTypeDefinition:
    try:
        return DATASET_REGISTRY[code]
    except KeyError as error:
        raise DatasetRegistryError("Unknown dataset type code.") from error


def validate_dataset_scope(*, dataset_type_code: str, station) -> DatasetTypeDefinition:
    definition = get_dataset_definition(dataset_type_code)
    if definition.scope == "department" and station is not None:
        raise DatasetRegistryError("Department-scoped datasets cannot have a station.")
    if definition.scope == "station" and station is None:
        raise DatasetRegistryError("Station-scoped datasets require a station.")
    return definition
