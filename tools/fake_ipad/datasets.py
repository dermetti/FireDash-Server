"""Plaintext dataset-format validation for decrypted publication artifacts.

These are pure format validators: they receive decrypted plaintext plus the
verified manifest entry and assert the documented on-disk formats (GeoJSON
hydrants, ZIP fire plans, JSON personnel). No HTTP and no key material here.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from types import MappingProxyType
from typing import Any

from tools.fake_ipad.crypto import sha256_hex
from tools.fake_ipad.errors import fail
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import secure_write
from tools.fake_ipad.validation import require_keys, require_uuid

_EXTENSIONS = {
    "department_hydrants": "geojson",
    "department_fire_plans": "zip",
    "station_personnel": "json",
}

# This is intentionally the capability table of the current fake/iOS-equivalent
# client, not a reflection of the server registry. Unknown optional entries are
# skipped only after their signed manifest entry has been authenticated.
SUPPORTED_DATASETS = MappingProxyType(
    {
        "department_hydrants": ("geojson", frozenset({1})),
        "department_fire_plans": ("zip", frozenset({1})),
        "station_personnel": ("json", frozenset({1})),
    }
)


def save_plaintext_artifact(*, state_dir: Path, dataset_type: str, plaintext: bytes) -> None:
    output_dir = state_dir / "last-plaintext"
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = _EXTENSIONS.get(dataset_type, "bin")
    secure_write(output_dir / f"{dataset_type}.{extension}", plaintext)


def validate_hydrants(plaintext: bytes, dataset: dict[str, Any], out: Output) -> dict[str, Any]:
    try:
        doc = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        fail(f"department_hydrants: invalid UTF-8 JSON: {exc}")
    if not isinstance(doc, dict):
        fail("department_hydrants: top-level JSON must be an object")
    if doc.get("type") != "FeatureCollection":
        fail("department_hydrants: GeoJSON type must be FeatureCollection")
    if doc.get("schema_version") != dataset["schema_version"]:
        fail("department_hydrants: schema_version differs from manifest")
    if not isinstance(doc.get("source_revision"), int):
        fail("department_hydrants: source_revision must be integer")
    features = doc.get("features")
    if not isinstance(features, list):
        fail("department_hydrants: features must be array")

    status_counts: dict[str, int] = {}
    diameters: list[int] = []
    sample: list[str] = []

    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            fail(f"department_hydrants.features[{index}] must be object")
        if feature.get("type") != "Feature":
            fail(f"department_hydrants.features[{index}] type must be Feature")
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            fail(f"department_hydrants.features[{index}] must use Point geometry")
        coordinates = geometry.get("coordinates")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
            or not all(isinstance(v, int | float) for v in coordinates[:2])
        ):
            fail(f"department_hydrants.features[{index}] coordinates must be [longitude, latitude]")
        lon, lat = coordinates[:2]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            fail(f"department_hydrants.features[{index}] coordinates outside lon/lat bounds")
        if not isinstance(properties, dict):
            fail(f"department_hydrants.features[{index}].properties must be object")
        require_keys(
            properties,
            ["external_identifier", "hydrant_type", "diameter_mm", "status"],
            label=f"department_hydrants.features[{index}].properties",
        )

        status_value = str(properties["status"])
        status_counts[status_value] = status_counts.get(status_value, 0) + 1

        diameter = properties["diameter_mm"]
        if diameter is not None:
            if not isinstance(diameter, int) or diameter <= 0:
                fail(
                    f"department_hydrants.features[{index}].diameter_mm "
                    "must be a positive integer or null"
                )
            diameters.append(diameter)

        if len(sample) < 5:
            sample.append(
                f"{properties['external_identifier']} | "
                f"{properties['hydrant_type']} | "
                f"DN{diameter if diameter is not None else '?'} | "
                f"{status_value} | {lon},{lat}"
            )

    out.line("\n  [HYDRANT GEOJSON]")
    out.line(f"    source revision: {doc['source_revision']}")
    out.line(f"    features:        {len(features)}")
    out.line(f"    statuses:        {status_counts}")
    if diameters:
        out.line(f"    diameter range:  {min(diameters)}–{max(diameters)} mm")
    if sample:
        out.line("    sample:")
        for line in sample:
            out.line(f"      {line}")

    return {
        "source_revision": doc["source_revision"],
        "features": len(features),
        "status_counts": status_counts,
    }


def validate_personnel(
    plaintext: bytes, dataset: dict[str, Any], config: dict[str, Any], out: Output
) -> dict[str, Any]:
    try:
        doc = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        fail(f"station_personnel: invalid UTF-8 JSON: {exc}")
    if not isinstance(doc, dict):
        fail("station_personnel: top-level JSON must be object")
    require_keys(doc, ["station_id", "source_revision", "people"], label="station_personnel")
    if doc["station_id"] != config["station_id"]:
        fail("station_personnel: station_id differs from tablet configuration")
    if not isinstance(doc["source_revision"], int):
        fail("station_personnel: source_revision must be integer")
    if not isinstance(doc["people"], list):
        fail("station_personnel: people must be array")

    commanders = 0
    verified_email_count = 0
    for index, person in enumerate(doc["people"]):
        if not isinstance(person, dict):
            fail(f"station_personnel.people[{index}] must be object")
        require_keys(
            person,
            ["id", "display_name", "incident_commander_eligible", "commander_email"],
            label=f"station_personnel.people[{index}]",
        )
        require_uuid(person["id"], label=f"station_personnel.people[{index}].id")
        if not isinstance(person["display_name"], str):
            fail(f"station_personnel.people[{index}].display_name must be string")
        if not isinstance(person["incident_commander_eligible"], bool):
            fail(f"station_personnel.people[{index}].incident_commander_eligible must be boolean")
        if person["commander_email"] is not None and not isinstance(person["commander_email"], str):
            fail(f"station_personnel.people[{index}].commander_email must be string or null")
        commanders += int(person["incident_commander_eligible"])
        verified_email_count += int(person["commander_email"] is not None)

    out.line("\n  [STATION PERSONNEL JSON]")
    out.line(f"    station:          {doc['station_id']}")
    out.line(f"    source revision:  {doc['source_revision']}")
    out.line(f"    people:           {len(doc['people'])}")
    out.line(f"    commander eligible:{commanders}")
    out.line(f"    commander emails: {verified_email_count}")

    return {
        "source_revision": doc["source_revision"],
        "people": len(doc["people"]),
        "commander_eligible": commanders,
    }


def validate_fire_plans(plaintext: bytes, dataset: dict[str, Any], out: Output) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(plaintext), "r")
    except Exception as exc:
        fail(f"department_fire_plans: plaintext is not a valid ZIP: {exc}")

    with archive:
        names = archive.namelist()
        if "manifest.json" not in names:
            fail("department_fire_plans: ZIP missing manifest.json")

        for info in archive.infolist():
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts:
                fail(f"department_fire_plans: unsafe ZIP path {info.filename!r}")
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                fail(f"department_fire_plans: symlink entry {info.filename!r}")

        try:
            internal_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            fail(f"department_fire_plans: invalid manifest.json: {exc}")

        if not isinstance(internal_manifest, dict):
            fail("department_fire_plans: manifest.json must be an object")
        require_keys(
            internal_manifest, ["source_revision", "fire_plans"], label="fire-plan manifest.json"
        )
        if not isinstance(internal_manifest["source_revision"], int):
            fail("fire-plan manifest source_revision must be integer")
        plans = internal_manifest["fire_plans"]
        if not isinstance(plans, list):
            fail("fire-plan manifest fire_plans must be array")

        referenced_paths: set[str] = set()
        with_address = 0
        with_location = 0

        for index, plan in enumerate(plans):
            if not isinstance(plan, dict):
                fail(f"fire_plans[{index}] must be object")
            require_keys(
                plan,
                [
                    "id",
                    "external_identifier",
                    "object_name",
                    "address",
                    "postal_code",
                    "city",
                    "longitude",
                    "latitude",
                    "sha256",
                    "page_count",
                    "path",
                ],
                label=f"fire_plans[{index}]",
            )
            require_uuid(plan["id"], label=f"fire_plans[{index}].id")
            for field in ("external_identifier", "object_name", "address", "postal_code", "city"):
                value = plan[field]
                if value is not None and not isinstance(value, str):
                    fail(f"fire_plans[{index}].{field} must be string or null")
            longitude = plan["longitude"]
            latitude = plan["latitude"]
            if (longitude is None) != (latitude is None):
                fail(f"fire_plans[{index}] longitude and latitude must be paired or both null")
            if longitude is not None:
                if not isinstance(longitude, int | float) or isinstance(longitude, bool):
                    fail(f"fire_plans[{index}].longitude must be numeric or null")
                if not isinstance(latitude, int | float) or isinstance(latitude, bool):
                    fail(f"fire_plans[{index}].latitude must be numeric or null")
                if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                    fail(f"fire_plans[{index}] coordinates outside lon/lat bounds")
            if not re.fullmatch(r"[0-9a-f]{64}", plan["sha256"]):
                fail(f"fire_plans[{index}].sha256 malformed")
            if not isinstance(plan["page_count"], int) or plan["page_count"] <= 0:
                fail(f"fire_plans[{index}].page_count must be positive integer")
            path_string = plan["path"]
            if not isinstance(path_string, str):
                fail(f"fire_plans[{index}].path must be string")
            path = Path(path_string)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path_string.startswith("plans/")
                or not path_string.endswith(".pdf")
            ):
                fail(f"fire_plans[{index}].path unsafe: {path_string!r}")
            if path_string not in names:
                fail(f"fire_plans[{index}] references missing PDF {path_string!r}")
            if path_string in referenced_paths:
                fail(f"Duplicate fire-plan path {path_string!r}")
            referenced_paths.add(path_string)

            pdf = archive.read(path_string)
            if sha256_hex(pdf) != plan["sha256"]:
                fail(f"Fire-plan PDF SHA-256 mismatch: {path_string}")

            with_address += int(bool(plan["address"]))
            with_location += int(longitude is not None)

        pdf_files = {
            name for name in names if name.startswith("plans/") and name.lower().endswith(".pdf")
        }
        unexpected_pdfs = sorted(pdf_files - referenced_paths)
        if unexpected_pdfs:
            fail(
                "department_fire_plans: ZIP contains PDFs not referenced by manifest: "
                + ", ".join(unexpected_pdfs[:5])
            )

        out.line("\n  [FIRE-PLAN ZIP]")
        out.line(f"    source revision:      {internal_manifest['source_revision']}")
        out.line(f"    manifest plans:       {len(plans)}")
        out.line(f"    PDF files:            {len(pdf_files)}")
        out.line(f"    per-PDF hashes:       PASS ({len(plans)}/{len(plans)})")
        out.line(f"    entries with address: {with_address}")
        out.line(f"    entries with location:{with_location}")

        return {
            "source_revision": internal_manifest["source_revision"],
            "fire_plans": len(plans),
            "with_address": with_address,
            "with_location": with_location,
        }


def validate_plaintext(
    plaintext: bytes,
    dataset: dict[str, Any],
    config: dict[str, Any],
    out: Output,
    *,
    save_plaintext: bool,
    state_dir: Path,
) -> dict[str, Any]:
    dataset_type = dataset["type"]

    if save_plaintext:
        save_plaintext_artifact(state_dir=state_dir, dataset_type=dataset_type, plaintext=plaintext)

    if dataset_type == "department_hydrants":
        if dataset["artifact_format"] != "geojson":
            fail("department_hydrants artifact_format must be geojson")
        return validate_hydrants(plaintext, dataset, out)

    if dataset_type == "department_fire_plans":
        if dataset["artifact_format"] != "zip":
            fail("department_fire_plans artifact_format must be zip")
        return validate_fire_plans(plaintext, dataset, out)

    if dataset_type == "station_personnel":
        if dataset["artifact_format"] != "json":
            fail("station_personnel artifact_format must be json")
        return validate_personnel(plaintext, dataset, config, out)

    out.line(
        f"\n  [UNKNOWN DATASET {dataset_type}] "
        f"{len(plaintext)} plaintext bytes; generic crypto checks passed"
    )
    return {"plaintext_bytes": len(plaintext), "sha256": sha256_hex(plaintext)}
