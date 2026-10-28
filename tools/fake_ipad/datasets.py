"""Plaintext dataset-format validation for decrypted publication artifacts.

These are pure format validators: they receive decrypted plaintext plus the
verified manifest entry and assert the documented on-disk formats (GeoJSON
hydrants, historical ZIP fire plans, JSON personnel, phonebooks and dangerous goods).
Document-v2 snapshots are handled in ``generations``. No HTTP or key material here.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from tools.fake_ipad.crypto import sha256_hex
from tools.fake_ipad.errors import fail
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import secure_write
from tools.fake_ipad.validation import require_keys, require_uuid

_EXTENSIONS = {
    "department_hydrants": "geojson",
    "department_fire_plans": "zip",
    "station_personnel": "json",
    "dangerous_goods": "json",
    "department_phonebook": "json",
    "station_phonebook": "json",
}

# This is intentionally the capability table of the current fake/iOS-equivalent
# client, not a reflection of the server registry. Unknown optional entries are
# skipped only after their signed manifest entry has been authenticated.
SUPPORTED_DATASETS = MappingProxyType(
    {
        "department_hydrants": frozenset({("geojson", 1)}),
        "department_fire_plans": frozenset({("zip", 1), ("document-manifest-v2", 2)}),
        "station_personnel": frozenset({("json", 1)}),
        "dangerous_goods": frozenset({("json", 1)}),
        "department_phonebook": frozenset({("json", 1)}),
        "station_phonebook": frozenset({("json", 1)}),
        "department_files": frozenset({("document-manifest-v2", 2)}),
        "station_files": frozenset({("document-manifest-v2", 2)}),
        "department_klgv_plans": frozenset({("document-manifest-v2", 2)}),
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
            if type(diameter) is not int or diameter < 0:
                fail(
                    f"department_hydrants.features[{index}].diameter_mm "
                    "must be a non-negative integer or null"
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


# Standalone mirror of apps/ingestion/dangerous_goods.py: no Django/server imports.
_DG_UN = re.compile(r"^[0-9]{4}$")
_DG_ADR_STRING_FIELDS = {
    "class",
    "classification_code",
    "hazard_identification_number",
    "packing_group",
}


def _dg_fail(message: str) -> NoReturn:
    fail(f"dangerous_goods: {message}")


def _dg_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _dg_fail(f"{label} must be an object.")
    return value


def _dg_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _dg_fail(f"{label} must be a non-empty string.")
    return value


def _dg_validate_placard(value: Any, assets: dict[str, Any]) -> None:
    if isinstance(value, str):
        if value not in assets:
            _dg_fail("Placard string does not resolve through the asset catalog.")
        return
    placard = _dg_mapping(value, "Placard")
    kind = placard.get("kind")
    if kind == "conditional":
        if (
            set(placard) != {"kind", "code"}
            or not isinstance(placard["code"], str)
            or placard["code"] not in assets
        ):
            _dg_fail("Conditional placard must reference one catalog asset.")
    elif kind == "variable":
        if set(placard) != {"kind", "candidate_codes", "selection_basis"} or not isinstance(
            placard["selection_basis"], str
        ):
            _dg_fail("Variable placard is invalid.")
        # 7X is deliberately a three-candidate contract; never add/infer 7D.
        if placard["candidate_codes"] != ["7A", "7B", "7C"] or any(
            code not in assets for code in placard["candidate_codes"]
        ):
            _dg_fail("Variable placard must use exactly 7A, 7B, and 7C.")
    elif kind == "none":
        if set(placard) != {"kind"}:
            _dg_fail("None placard is invalid.")
    elif kind == "reference":
        if set(placard) != {"kind", "reference"}:
            _dg_fail("Reference placard is invalid.")
        _dg_nonempty_string(placard["reference"], "Reference placard")
    else:
        _dg_fail("Placard object kind is invalid.")


def _dg_valid_remarks(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(language, str)
        and isinstance(texts, list)
        and all(isinstance(text, str) for text in texts)
        for language, texts in value.items()
    )


def validate_dangerous_goods(
    payload: bytes, dataset: dict[str, Any], out: Output
) -> dict[str, Any]:
    """Validate compact schema 1 without rewriting or normalizing source bytes."""
    try:
        document = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda value: _dg_fail(f"Invalid JSON constant: {value}."),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _dg_fail(f"Source must be UTF-8 JSON: {error}")
    root = _dg_mapping(document, "Document")
    if root.get("dataset_type") != "dangerous_goods" or root.get("schema_version") != 1:
        _dg_fail("Document must be dangerous_goods schema version 1.")
    if (
        type(root["schema_version"]) is not int
        or type(dataset["schema_version"]) is not int
        or dataset["schema_version"] != 1
    ):
        _dg_fail("Unsupported schema version; expected 1.")
    metadata = _dg_mapping(root.get("metadata"), "metadata")
    if metadata.get("publication_profile") != "compact":
        _dg_fail("metadata.publication_profile must be compact.")
    catalog = _dg_mapping(metadata.get("placard_catalog"), "metadata.placard_catalog")
    assets = _dg_mapping(
        catalog.get("available_assets"), "metadata.placard_catalog.available_assets"
    )
    if any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in assets.items()
    ):
        _dg_fail("Placard asset catalog is invalid.")
    special_values = _dg_mapping(
        catalog.get("special_values"), "metadata.placard_catalog.special_values"
    )
    seven_x = special_values.get("7X")
    if (
        not isinstance(seven_x, dict)
        or seven_x.get("kind") != "variable"
        or seven_x.get("candidate_codes") != ["7A", "7B", "7C"]
    ):
        _dg_fail("Declared 7X placard contract must use exactly 7A, 7B, and 7C.")
    goods = root.get("goods")
    cards = _dg_mapping(root.get("eri_cards"), "eri_cards")
    if not isinstance(goods, list):
        _dg_fail("goods must be a list.")
    if any(type(metadata.get(key)) is not int for key in ("record_count", "eri_card_count")):
        _dg_fail("Metadata counts must be integers.")
    if metadata.get("record_count") != len(goods) or metadata.get("eri_card_count") != len(cards):
        _dg_fail("Metadata counts do not match the document.")
    ids: set[str] = set()
    for item in goods:
        good = _dg_mapping(item, "goods entry")
        identifier = _dg_nonempty_string(good.get("id"), "goods.id")
        if identifier in ids:
            _dg_fail("goods IDs must be unique.")
        ids.add(identifier)
        un = good.get("un_number")
        if not isinstance(un, str) or not _DG_UN.fullmatch(un):
            _dg_fail("UN number must be a four-digit string.")
        names = _dg_mapping(good.get("names"), "names")
        official = _dg_mapping(names.get("official"), "names.official")
        if not official or any(
            not isinstance(language, str) or not isinstance(name, str) or not name.strip()
            for language, name in official.items()
        ):
            _dg_fail("names.official must be a non-empty language-keyed string mapping.")
        aliases = names.get("aliases")
        if aliases is not None and (
            not isinstance(aliases, dict)
            or any(
                not isinstance(language, str)
                or not isinstance(values, list)
                or any(not isinstance(name, str) for name in values)
                for language, values in aliases.items()
            )
        ):
            _dg_fail("names.aliases must be language-keyed string arrays.")
        adr = good.get("adr")
        if adr is not None:
            adr = _dg_mapping(adr, "adr")
            if any(
                field in adr and not isinstance(adr[field], str) for field in _DG_ADR_STRING_FIELDS
            ):
                _dg_fail("ADR field type is invalid.")
            if "remarks" in adr and not _dg_valid_remarks(adr["remarks"]):
                _dg_fail("ADR remarks type is invalid.")
            if "placards" in adr:
                if not isinstance(adr["placards"], list):
                    _dg_fail("ADR placards must be a list.")
                for placard in adr["placards"]:
                    _dg_validate_placard(placard, assets)
        eri = good.get("eri")
        if eri not in (None, []) and (
            not isinstance(eri, list)
            or any(not isinstance(code, str) or code not in cards for code in eri)
        ):
            _dg_fail("ERI references must point to existing ERI cards.")
    defaults = _dg_mapping(root.get("eri_defaults"), "eri_defaults")
    if any(
        not isinstance(un, str)
        or not _DG_UN.fullmatch(un)
        or not isinstance(code, str)
        or code not in cards
        for un, code in defaults.items()
    ):
        _dg_fail("ERI defaults must reference valid UN numbers and cards.")
    for code, entries in cards.items():
        if not isinstance(code, str) or not isinstance(entries, list) or not entries:
            _dg_fail("ERI cards must be non-empty lists.")
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or entry[0] not in {"title", "heading", "item"}
                or not isinstance(entry[1], str)
            ):
                _dg_fail("ERI card entries must be [kind, text] pairs.")
            if index == 0 and entry[0] != "title":
                _dg_fail("ERI cards must start with a title.")
    sources = root.get("sources")
    if not isinstance(sources, list):
        _dg_fail("Sources must be a list.")
    source_by_id = {
        source.get("id"): source
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    for source_id in ("bam", "ericards"):
        source = source_by_id.get(source_id)
        if not isinstance(source, dict) or not isinstance(source.get("legal"), dict):
            _dg_fail("Required BAM and ERICards provenance/legal structures are missing.")
        if not all(
            isinstance(source.get(key), str) and source[key]
            for key in ("provider", "dataset", "source_file", "sha256", "source_url")
        ):
            _dg_fail("Required source provenance is incomplete.")
        legal = source["legal"]
        if source_id == "bam" and not (
            isinstance(legal.get("legal_url"), str)
            and isinstance(legal.get("license"), dict)
            and isinstance(legal.get("attribution"), dict)
            and isinstance(legal.get("processing"), dict)
        ):
            _dg_fail("BAM legal source structure is incomplete.")
        if source_id == "ericards" and not (
            all(
                isinstance(legal.get(key), str)
                for key in ("terms_url", "guidance_url", "disclaimer_url")
            )
            and isinstance(legal.get("attribution"), dict)
            and isinstance(legal.get("reproduction"), dict)
        ):
            _dg_fail("ERICards legal source structure is incomplete.")
    summary = {
        "record_count": len(goods),
        "eri_card_count": len(cards),
        "adr_edition": metadata.get("adr_edition"),
        "ericards_version": metadata.get("ericards_version"),
        "sha256": sha256_hex(payload),
    }
    out.line("\n  [DANGEROUS GOODS JSON]")
    for key, value in summary.items():
        out.line(f"    {key}: {value if value is not None else '(not supplied)'}")
    return summary


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

    if dataset_type == "dangerous_goods":
        if dataset["artifact_format"] != "json":
            fail("dangerous_goods artifact_format must be json")
        return validate_dangerous_goods(plaintext, dataset, out)

    if dataset_type == "station_personnel":
        if dataset["artifact_format"] != "json":
            fail("station_personnel artifact_format must be json")
        return validate_personnel(plaintext, dataset, config, out)

    if dataset_type in {"department_phonebook", "station_phonebook"}:
        return validate_phonebook(plaintext, out)

    out.line(
        f"\n  [UNKNOWN DATASET {dataset_type}] "
        f"{len(plaintext)} plaintext bytes; generic crypto checks passed"
    )
    return {"plaintext_bytes": len(plaintext), "sha256": sha256_hex(plaintext)}


def validate_phonebook(plaintext: bytes, out: Output) -> dict[str, Any]:
    from tools.fake_ipad.generations import exact, identity, integer

    try:
        doc = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("phonebook: invalid UTF-8 JSON")
    exact(doc, {"entries", "source_revision"}, "phonebook")
    integer(doc["source_revision"], "phonebook.source_revision")
    if not isinstance(doc["entries"], list):
        fail("phonebook.entries: expected array")
    seen = set()
    for entry in doc["entries"]:
        exact(
            entry,
            {"id", "first_name", "last_name", "organization_unit", "function", "phone_number"},
            "phonebook entry",
        )
        entry_id = identity(entry["id"], "phonebook.id")
        if entry_id in seen:
            fail("phonebook: duplicate entry identity")
        seen.add(entry_id)
        for field in ("first_name", "last_name", "organization_unit", "function"):
            if entry[field] is not None and not isinstance(entry[field], str):
                fail(f"phonebook.{field}: expected string or null")
        if not isinstance(entry["phone_number"], str) or not entry["phone_number"].strip():
            fail("phonebook.phone_number: expected nonempty string")
    out.line(f"  phonebook entries: PASS ({len(seen)})")
    return {"entries": len(seen), "source_revision": doc["source_revision"]}
