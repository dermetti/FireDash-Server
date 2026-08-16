from dataclasses import dataclass
from types import MappingProxyType


class FeatureRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureDefinition:
    code: str
    display_name: str
    description: str
    default_enabled: bool = True


FEATURE_REGISTRY = MappingProxyType(
    {
        "publications": FeatureDefinition(
            code="publications",
            display_name="Dataset publications",
            description="Build and review department dataset publications.",
        ),
        "klgv_plans": FeatureDefinition(
            code="klgv_plans",
            display_name="KLGV plan publications",
            description="Optional future KLGV PDF document bundles.",
            default_enabled=False,
        ),
    }
)


def get_feature_definition(code: str) -> FeatureDefinition:
    try:
        return FEATURE_REGISTRY[code]
    except KeyError as error:
        raise FeatureRegistryError("Unknown feature code.") from error
