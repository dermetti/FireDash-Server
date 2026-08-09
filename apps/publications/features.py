from dataclasses import dataclass
from types import MappingProxyType


class FeatureRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureDefinition:
    code: str
    display_name: str
    description: str


FEATURE_REGISTRY = MappingProxyType(
    {
        "publications": FeatureDefinition(
            code="publications",
            display_name="Dataset publications",
            description="Build and review department dataset publications.",
        )
    }
)


def get_feature_definition(code: str) -> FeatureDefinition:
    try:
        return FEATURE_REGISTRY[code]
    except KeyError as error:
        raise FeatureRegistryError("Unknown feature code.") from error
