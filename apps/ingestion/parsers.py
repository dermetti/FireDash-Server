"""Strict bounded parsers for the initially supported structured imports."""

import csv
import io
import json
import math
from collections.abc import Mapping
from typing import cast

from django.conf import settings


class ImportValidationError(ValueError):
    pass


HYDRANT_FIELDS = frozenset(
    {"external_identifier", "longitude", "latitude", "hydrant_type", "diameter_mm", "status"}
)
HYDRANT_GEOJSON_PROPERTY_FIELDS = HYDRANT_FIELDS - {"longitude", "latitude"}
PERSONNEL_FIELDS = frozenset(
    {"personnel_number", "first_name", "last_name", "incident_commander_eligible"}
)


def parse_hydrants(*, payload: bytes, import_format: str) -> list[dict[str, object]]:
    if import_format == "geojson":
        return _geojson_hydrants(payload)
    rows = _rows(payload, import_format, HYDRANT_FIELDS)
    result = []
    for number, row in enumerate(rows, 2):
        identifier = _required(row, "external_identifier", number)
        longitude = _coordinate(row, "longitude", number, -180, 180)
        latitude = _coordinate(row, "latitude", number, -90, 90)
        result.append(
            {
                "external_identifier": identifier,
                "longitude": longitude,
                "latitude": latitude,
                "hydrant_type": _optional_text(row.get("hydrant_type", ""), 128, number),
                "diameter_mm": _optional_positive_int(row.get("diameter_mm"), number),
                "status": _optional_text(row.get("status", "ACTIVE"), 128, number) or "ACTIVE",
            }
        )
    _unique(result, "external_identifier")
    return result


def parse_personnel(*, payload: bytes, import_format: str) -> list[dict[str, object]]:
    rows = _rows(payload, import_format, PERSONNEL_FIELDS)
    result = []
    for number, row in enumerate(rows, 2):
        eligible = row.get("incident_commander_eligible", False)
        if isinstance(eligible, str):
            if eligible not in {"true", "false", ""}:
                raise ImportValidationError(f"Row {number}: invalid incident_commander_eligible.")
            eligible = eligible == "true"
        if type(eligible) is not bool:
            raise ImportValidationError(f"Row {number}: invalid incident_commander_eligible.")
        result.append(
            {
                "personnel_number": _required(row, "personnel_number", number),
                "first_name": _required(row, "first_name", number),
                "last_name": _required(row, "last_name", number),
                "incident_commander_eligible": eligible,
            }
        )
    _unique(result, "personnel_number")
    return result


def _rows(payload: bytes, import_format: str, fields: frozenset[str]) -> list[Mapping[str, object]]:
    if len(payload) > settings.MAX_STRUCTURED_IMPORT_BYTES:
        raise ImportValidationError("Structured import exceeds the configured size limit.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ImportValidationError("Import must be UTF-8.") from error
    rows: list[Mapping[str, object]]
    if import_format == "csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or set(reader.fieldnames) != fields:
            raise ImportValidationError("CSV columns do not match the documented schema.")
        rows = list(reader)
    elif import_format == "json":
        try:
            rows = cast(list[Mapping[str, object]], json.loads(text))
        except json.JSONDecodeError as error:
            raise ImportValidationError("JSON import is invalid.") from error
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) or set(row) != fields for row in rows
        ):
            raise ImportValidationError("JSON objects do not match the documented schema.")
    else:
        raise ImportValidationError("Unsupported structured import format.")
    if len(rows) > settings.MAX_STRUCTURED_IMPORT_ROWS:
        raise ImportValidationError("Structured import row limit exceeded.")
    return rows


def _geojson_hydrants(payload: bytes) -> list[dict[str, object]]:
    if len(payload) > settings.MAX_STRUCTURED_IMPORT_BYTES:
        raise ImportValidationError("Structured import exceeds the configured size limit.")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportValidationError("GeoJSON import must be UTF-8 JSON.") from error
    if (
        not isinstance(document, Mapping)
        or document.get("type") != "FeatureCollection"
        or set(document) != {"type", "features"}
    ):
        raise ImportValidationError("GeoJSON must be a FeatureCollection.")
    features = document.get("features")
    if not isinstance(features, list) or len(features) > settings.MAX_HYDRANT_GEOJSON_FEATURES:
        raise ImportValidationError("GeoJSON feature limit exceeded.")
    rows = []
    for number, feature in enumerate(features, 1):
        if (
            not isinstance(feature, Mapping)
            or set(feature) != {"type", "geometry", "properties"}
            or feature.get("type") != "Feature"
        ):
            raise ImportValidationError(f"Feature {number}: invalid feature.")
        geometry, properties = feature["geometry"], feature["properties"]
        if (
            not isinstance(geometry, Mapping)
            or geometry.get("type") != "Point"
            or set(geometry) != {"type", "coordinates"}
        ):
            raise ImportValidationError(f"Feature {number}: geometry must be Point.")
        if (
            not isinstance(properties, Mapping)
            or set(properties) != HYDRANT_GEOJSON_PROPERTY_FIELDS
        ):
            raise ImportValidationError(f"Feature {number}: properties do not match schema.")
        coordinates = geometry["coordinates"]
        if not isinstance(coordinates, list) or len(coordinates) != 2:
            raise ImportValidationError(f"Feature {number}: coordinates are invalid.")
        row = dict(properties)
        row["longitude"], row["latitude"] = coordinates
        rows.append(
            {
                "external_identifier": _required(row, "external_identifier", number),
                "longitude": _coordinate(row, "longitude", number, -180, 180),
                "latitude": _coordinate(row, "latitude", number, -90, 90),
                "hydrant_type": _optional_text(row["hydrant_type"], 128, number),
                "diameter_mm": _optional_positive_int(row["diameter_mm"], number),
                "status": _optional_text(row["status"], 128, number) or "ACTIVE",
            }
        )
    _unique(rows, "external_identifier")
    return rows


def _required(row, field, number) -> str:
    return _optional_text(row.get(field), 255, number) or _raise(
        f"Row {number}: {field} is required."
    )


def _optional_text(value, maximum, number) -> str:
    if not isinstance(value, str) or len(value.strip()) > maximum:
        raise ImportValidationError(f"Row {number}: text field is invalid.")
    return value.strip()


def _coordinate(row, field, number, lower, upper) -> float:
    value = row.get(field)
    if type(value) not in {int, float, str}:
        raise ImportValidationError(f"Row {number}: {field} is invalid.")
    try:
        number_value = float(value)
    except (TypeError, ValueError):
        raise ImportValidationError(f"Row {number}: {field} is invalid.") from None
    if not math.isfinite(number_value) or not lower <= number_value <= upper:
        raise ImportValidationError(f"Row {number}: {field} is out of range.")
    return number_value


def _optional_positive_int(value, number):
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        raise ImportValidationError(f"Row {number}: integer field is invalid.")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ImportValidationError(f"Row {number}: integer field is invalid.") from None
    if parsed < 0:
        raise ImportValidationError(f"Row {number}: integer field is invalid.")
    return parsed


def _unique(rows, field):
    values = [row[field] for row in rows]
    if len(values) != len(set(values)):
        raise ImportValidationError(f"Duplicate {field} in import.")


def _raise(message):
    raise ImportValidationError(message)
