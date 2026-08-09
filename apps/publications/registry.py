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
                "active_count": "non_negative_integer",
                "source_revision": "non_negative_integer",
                "status_counts": "bounded_string_integer_map",
            }
        ),
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
                "active_document_count": "non_negative_integer",
                "total_accepted_bytes": "non_negative_integer",
                "total_pages": "non_negative_integer",
                "source_revision": "non_negative_integer",
            }
        ),
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
                "person_count": "non_negative_integer",
                "station_id": "uuid",
                "commander_eligible_count": "non_negative_integer",
                "verified_commander_email_count": "non_negative_integer",
                "source_revision": "non_negative_integer",
            }
        ),
    ),
)

DATASET_REGISTRY = MappingProxyType({definition.code: definition for definition in _DEFINITIONS})


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
