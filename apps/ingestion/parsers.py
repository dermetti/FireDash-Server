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
    {
        "external_identifier",
        "longitude",
        "latitude",
        "street",
        "house_number",
        "hydrant_type",
        "diameter_mm",
        "status",
    }
)
HYDRANT_GEOJSON_PROPERTY_FIELDS = HYDRANT_FIELDS - {"longitude", "latitude"}
PERSONNEL_FIELDS = frozenset(
    {
        "personnel_number",
        "first_name",
        "last_name",
        "home_station",
        "incident_commander_eligible",
    }
)
STATION_VEHICLE_FIELDS = frozenset(
    {
        "row_type",
        "station_short_code",
        "station_name",
        "street",
        "house_number",
        "postal_code",
        "city",
        "vehicle_name",
        "vehicle_call_sign",
        "vehicle_asset_identifier",
    }
)


def parse_hydrants(*, payload: bytes, import_format: str) -> list[dict[str, object]]:
    if import_format == "geojson":
        return _geojson_hydrants(payload)
    if import_format != "csv":
        raise ImportValidationError("Hydrant imports use CSV or GeoJSON only.")
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
                "street": _optional_text(row.get("street", ""), 255, number),
                "house_number": _optional_text(row.get("house_number", ""), 32, number),
                "hydrant_type": _optional_text(row.get("hydrant_type", ""), 128, number),
                "diameter_mm": _optional_positive_int(row.get("diameter_mm"), number),
                "status": _optional_text(row.get("status", "ACTIVE"), 128, number) or "ACTIVE",
            }
        )
    _unique(result, "external_identifier")
    return result


def parse_personnel(*, payload: bytes, import_format: str) -> list[dict[str, object]]:
    if import_format != "csv":
        raise ImportValidationError("Personnel imports use CSV only.")
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
                "home_station": _optional_text(row.get("home_station", ""), 255, number),
                "incident_commander_eligible": eligible,
            }
        )
    _unique(result, "personnel_number")
    return result


def parse_station_vehicles(*, payload: bytes, import_format: str) -> list[dict[str, str]]:
    """Parse the one CSV contract used for Station and Vehicle staging."""
    rows = _rows(payload, import_format, STATION_VEHICLE_FIELDS)
    result: list[dict[str, str]] = []
    staged_station_codes: set[str] = set()
    staged_station_names: set[str] = set()
    for number, row in enumerate(rows, 2):
        row_type = _optional_text(row.get("row_type", ""), 32, number).casefold()
        if row_type not in {"station", "vehicle"}:
            raise ImportValidationError(f"Row {number}: row_type must be station or vehicle.")
        normalized = {
            "row_type": row_type,
            "station_short_code": _optional_text(row.get("station_short_code", ""), 64, number),
            "station_name": _optional_text(row.get("station_name", ""), 255, number),
            "street": _optional_text(row.get("street", ""), 255, number),
            "house_number": _optional_text(row.get("house_number", ""), 32, number),
            "postal_code": _optional_text(row.get("postal_code", ""), 32, number),
            "city": _optional_text(row.get("city", ""), 255, number),
            "vehicle_name": _optional_text(row.get("vehicle_name", ""), 255, number),
            "vehicle_call_sign": _optional_text(row.get("vehicle_call_sign", ""), 128, number),
            "vehicle_asset_identifier": _optional_text(
                row.get("vehicle_asset_identifier", ""), 128, number
            ),
        }
        if row_type == "station":
            if not normalized["station_short_code"] or not normalized["station_name"]:
                raise ImportValidationError(
                    f"Row {number}: Station rows require station_short_code and station_name."
                )
            if normalized["vehicle_name"]:
                raise ImportValidationError(
                    f"Row {number}: Station rows cannot include vehicle_name."
                )
            code_key = _station_reference_key(normalized["station_short_code"])
            name_key = _station_reference_key(normalized["station_name"])
            if code_key in staged_station_codes or name_key in staged_station_names:
                raise ImportValidationError(f"Row {number}: duplicate staged Station.")
            staged_station_codes.add(code_key)
            staged_station_names.add(name_key)
        else:
            if not normalized["vehicle_name"]:
                raise ImportValidationError(f"Row {number}: Vehicle rows require vehicle_name.")
            if not normalized["station_short_code"] and not normalized["station_name"]:
                raise ImportValidationError(
                    f"Row {number}: Vehicle rows require station_short_code or station_name."
                )
        result.append(normalized)
    return result


def _rows(
    payload: bytes,
    import_format: str,
    schemas: frozenset[str] | tuple[frozenset[str], ...],
) -> list[Mapping[str, object]]:
    accepted_schemas = (schemas,) if isinstance(schemas, frozenset) else schemas
    if len(payload) > settings.MAX_STRUCTURED_IMPORT_BYTES:
        raise ImportValidationError("Structured import exceeds the configured size limit.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ImportValidationError("Import must be UTF-8.") from error
    rows: list[Mapping[str, object]]
    if import_format == "csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or frozenset(reader.fieldnames) not in accepted_schemas:
            raise ImportValidationError("CSV columns do not match the documented schema.")
        rows = list(reader)
    elif import_format == "json":
        try:
            rows = cast(list[Mapping[str, object]], json.loads(text))
        except json.JSONDecodeError as error:
            raise ImportValidationError("JSON import is invalid.") from error
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) or frozenset(row) not in accepted_schemas for row in rows
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
            or frozenset(properties) != HYDRANT_GEOJSON_PROPERTY_FIELDS
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
                "street": _optional_text(row.get("street", ""), 255, number),
                "house_number": _optional_text(row.get("house_number", ""), 32, number),
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


def _station_reference_key(value: str) -> str:
    return " ".join(value.split()).casefold()


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
