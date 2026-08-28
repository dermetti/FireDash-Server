import json
from collections.abc import Mapping
from dataclasses import dataclass

from django.conf import settings


class HydrantImportError(ValueError):
    pass


SUPPORTED_PROPERTIES = {
    "external_identifier",
    "street",
    "house_number",
    "location",
    "hydrant_type",
    "diameter_mm",
    "status",
    "source_metadata",
}


@dataclass(frozen=True)
class NormalizedHydrant:
    longitude: float
    latitude: float
    external_identifier: str
    street: str
    house_number: str
    location: str | None
    hydrant_type: str
    diameter_mm: int | None
    status: str
    source_metadata: dict[str, str | int | bool]

    def as_json(self) -> dict[str, object]:
        return {
            "longitude": self.longitude,
            "latitude": self.latitude,
            "external_identifier": self.external_identifier,
            "street": self.street,
            "house_number": self.house_number,
            "location": self.location,
            "hydrant_type": self.hydrant_type,
            "diameter_mm": self.diameter_mm,
            "status": self.status,
            "source_metadata": self.source_metadata,
        }


def parse_feature_collection(raw: bytes) -> list[NormalizedHydrant]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HydrantImportError("Upload must be valid UTF-8 GeoJSON.") from error
    if not isinstance(document, Mapping) or document.get("type") != "FeatureCollection":
        raise HydrantImportError("Upload must be a GeoJSON FeatureCollection.")
    features = document.get("features")
    if not isinstance(features, list):
        raise HydrantImportError("GeoJSON FeatureCollection must contain a features array.")
    if len(features) > settings.MAX_HYDRANT_IMPORT_FEATURES:
        raise HydrantImportError("GeoJSON feature limit exceeded.")
    return [_parse_feature(feature) for feature in features]


def _parse_feature(feature: object) -> NormalizedHydrant:
    if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
        raise HydrantImportError("Each GeoJSON entry must be a Feature.")
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
        raise HydrantImportError("Hydrant geometry must be a Point.")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        raise HydrantImportError("Hydrant Point coordinates must contain longitude and latitude.")
    longitude, latitude = coordinates
    if (
        isinstance(longitude, bool)
        or isinstance(latitude, bool)
        or not isinstance(longitude, int | float)
        or not isinstance(latitude, int | float)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        raise HydrantImportError("Hydrant coordinates are invalid.")
    properties = feature.get("properties", {})
    if not isinstance(properties, Mapping) or set(properties) - SUPPORTED_PROPERTIES:
        raise HydrantImportError("Hydrant properties contain unsupported fields.")
    return NormalizedHydrant(
        longitude=float(longitude),
        latitude=float(latitude),
        external_identifier=_bounded_string(properties.get("external_identifier", ""), 255),
        street=_bounded_string(properties.get("street", ""), 255),
        house_number=_bounded_string(properties.get("house_number", ""), 32),
        location=_nullable_bounded_string(properties.get("location"), 255),
        hydrant_type=_bounded_string(properties.get("hydrant_type", ""), 128),
        diameter_mm=_bounded_int(properties.get("diameter_mm")),
        status=_bounded_string(properties.get("status", ""), 128),
        source_metadata=_metadata(properties.get("source_metadata", {})),
    )


def _bounded_string(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value.strip()) > maximum:
        raise HydrantImportError("Hydrant property is invalid or too long.")
    return value.strip()


def _nullable_bounded_string(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, maximum)


def _bounded_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise HydrantImportError("Hydrant diameter must be an integer.")
    try:
        return int(value)
    except ValueError:
        raise HydrantImportError("Hydrant diameter must be an integer.") from None


def _metadata(value: object) -> dict[str, str | int | bool]:
    if not isinstance(value, Mapping) or len(value) > 20:
        raise HydrantImportError("Hydrant source metadata is invalid.")
    normalized: dict[str, str | int | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64 or not isinstance(item, str | int | bool):
            raise HydrantImportError("Hydrant source metadata is invalid.")
        if isinstance(item, str) and len(item) > 255:
            raise HydrantImportError("Hydrant source metadata is invalid.")
        normalized[key] = item
    return normalized
