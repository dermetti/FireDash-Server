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
    # SYSTEM ownership never implies distribution. A future system dataset must
    # opt into an authorization policy explicitly.
    system_exposure: bool = False


_DEFINITIONS = (
    DatasetTypeDefinition(
        code="dangerous_goods",
        display_name="Dangerous goods",
        scope="department",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="dangerous_goods",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "goods_count": "item_count",
                "eri_card_count": "item_count",
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
        supported_schema_versions=(1, 2),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=2,
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
    DatasetTypeDefinition(
        code="department_phonebook",
        display_name="Department phonebook",
        scope="department",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="department_phonebook",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {"entry_count": "item_count", "source_revision": "non_negative_integer"}
        ),
        required=False,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=False,
    ),
    DatasetTypeDefinition(
        code="station_phonebook",
        display_name="Station phonebook",
        scope="station",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="station_phonebook",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {"entry_count": "item_count", "source_revision": "non_negative_integer"}
        ),
        required=False,
        supported_schema_versions=(1,),
        minimum_supported_schema_version=1,
        maximum_supported_schema_version=1,
        feature_code="publications",
        internal_only=False,
    ),
    # Production KLGV document collection. Department-level feature control
    # remains disabled by default so it is exposed only after a department has
    # uploaded plans and explicitly enabled the rollout.
    DatasetTypeDefinition(
        code="department_klgv_plans",
        display_name="Department KLGV plans",
        scope="department",
        artifact_format="document-manifest-v2",
        current_schema_version=2,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="department_klgv_plans",
        validator_service="summary",
        summary_schema=MappingProxyType(
            {
                "document_count": "item_count",
                "total_accepted_bytes": "non_negative_integer",
                "total_pages": "non_negative_integer",
                "source_revision": "non_negative_integer",
            }
        ),
        required=True,
        supported_schema_versions=(2,),
        minimum_supported_schema_version=2,
        maximum_supported_schema_version=2,
        feature_code="klgv_plans",
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
    DatasetTypeDefinition(
        code="test_system_dataset",
        display_name="Test system dataset",
        scope="system",
        artifact_format="json",
        current_schema_version=1,
        encryption_required=True,
        minimum_app_version=None,
        builder_service="test_system_dataset",
        validator_service="summary",
        summary_schema=MappingProxyType({"item_count": "item_count", "source_revision": "non_negative_integer"}),
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


def validate_dataset_scope(*, dataset_type_code: str, scope_type: str | None = None, department=None, station=None) -> DatasetTypeDefinition:
    definition = get_dataset_definition(dataset_type_code)
    expected_scope = definition.scope.upper()
    # Registry-only callers may ask whether a type accepts a station. Persisted
    # model/service paths always provide the explicit scope and owner fields.
    if scope_type is None:
        if expected_scope == "DEPARTMENT" and station is not None:
            raise DatasetRegistryError("Department-scoped datasets cannot have a station.")
        if expected_scope == "STATION" and station is None:
            raise DatasetRegistryError("Station-scoped datasets require a station.")
        return definition
    if scope_type != expected_scope:
        raise DatasetRegistryError("Dataset type is not registered for this publication scope.")
    if scope_type == "SYSTEM":
        if department is not None or station is not None:
            raise DatasetRegistryError("System-scoped datasets cannot have a department or station.")
    elif scope_type == "DEPARTMENT":
        if department is None or station is not None:
            raise DatasetRegistryError("Department-scoped datasets cannot have a station and require a department.")
    elif scope_type == "STATION":
        if department is None or station is None:
            raise DatasetRegistryError("Station scope requires a department and station.")
    else:
        raise DatasetRegistryError("Unknown publication scope type.")
    return definition
