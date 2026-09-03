"""Validation for the curated dangerous-goods v1 source file.

This deliberately validates the delivery contract without normalising it: the
caller retains and publishes the original UTF-8 bytes verbatim.
"""

import json
import re
from typing import Any


class DangerousGoodsValidationError(ValueError):
    pass


_UN = re.compile(r"^[0-9]{4}$")
_ADR_STRING_FIELDS = {
    "class",
    "classification_code",
    "hazard_identification_number",
    "packing_group",
}


def _fail(message: str) -> None:
    raise DangerousGoodsValidationError(message)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string.")
    return value


def _validate_placard(value: Any, assets: dict[str, Any]) -> None:
    if isinstance(value, str):
        if value not in assets:
            _fail("Placard string does not resolve through the asset catalog.")
        return
    placard = _mapping(value, "Placard")
    kind = placard.get("kind")
    if kind == "conditional":
        if (
            set(placard) != {"kind", "code"}
            or not isinstance(placard["code"], str)
            or placard["code"] not in assets
        ):
            _fail("Conditional placard must reference one catalog asset.")
    elif kind == "variable":
        if set(placard) != {"kind", "candidate_codes", "selection_basis"} or not isinstance(
            placard["selection_basis"], str
        ):
            _fail("Variable placard is invalid.")
        # 7X is deliberately a three-candidate contract; never add/infer 7D.
        if placard["candidate_codes"] != ["7A", "7B", "7C"] or any(
            code not in assets for code in placard["candidate_codes"]
        ):
            _fail("Variable placard must use exactly 7A, 7B, and 7C.")
    elif kind == "none":
        if set(placard) != {"kind"}:
            _fail("None placard is invalid.")
    elif kind == "reference":
        if set(placard) != {"kind", "reference"}:
            _fail("Reference placard is invalid.")
        _nonempty_string(placard["reference"], "Reference placard")
    else:
        _fail("Placard object kind is invalid.")


def _valid_remarks(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(language, str)
        and isinstance(texts, list)
        and all(isinstance(text, str) for text in texts)
        for language, texts in value.items()
    )


def validate_dangerous_goods(payload: bytes) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DangerousGoodsValidationError("Source must be UTF-8 JSON.") from error
    root = _mapping(document, "Document")
    if root.get("dataset_type") != "dangerous_goods" or root.get("schema_version") != 1:
        _fail("Document must be dangerous_goods schema version 1.")
    metadata = _mapping(root.get("metadata"), "metadata")
    if metadata.get("publication_profile") != "compact":
        _fail("metadata.publication_profile must be compact.")
    catalog = _mapping(metadata.get("placard_catalog"), "metadata.placard_catalog")
    assets = _mapping(catalog.get("available_assets"), "metadata.placard_catalog.available_assets")
    if any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in assets.items()
    ):
        _fail("Placard asset catalog is invalid.")
    special_values = _mapping(
        catalog.get("special_values"), "metadata.placard_catalog.special_values"
    )
    seven_x = special_values.get("7X")
    if (
        not isinstance(seven_x, dict)
        or seven_x.get("kind") != "variable"
        or seven_x.get("candidate_codes") != ["7A", "7B", "7C"]
    ):
        _fail("Declared 7X placard contract must use exactly 7A, 7B, and 7C.")
    goods = root.get("goods")
    cards = _mapping(root.get("eri_cards"), "eri_cards")
    if not isinstance(goods, list):
        _fail("goods must be a list.")
    if metadata.get("record_count") != len(goods) or metadata.get("eri_card_count") != len(cards):
        _fail("Metadata counts do not match the document.")
    ids: set[str] = set()
    for item in goods:
        good = _mapping(item, "goods entry")
        identifier = _nonempty_string(good.get("id"), "goods.id")
        if identifier in ids:
            _fail("goods IDs must be unique.")
        ids.add(identifier)
        un = good.get("un_number")
        if not isinstance(un, str) or not _UN.fullmatch(un):
            _fail("UN number must be a four-digit string.")
        names = _mapping(good.get("names"), "names")
        official = _mapping(names.get("official"), "names.official")
        if not official or any(
            not isinstance(language, str) or not isinstance(name, str) or not name.strip()
            for language, name in official.items()
        ):
            _fail("names.official must be a non-empty language-keyed string mapping.")
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
            _fail("names.aliases must be language-keyed string arrays.")
        adr = good.get("adr")
        if adr is not None:
            adr = _mapping(adr, "adr")
            if any(
                field in adr and not isinstance(adr[field], str) for field in _ADR_STRING_FIELDS
            ):
                _fail("ADR field type is invalid.")
            if "remarks" in adr and not _valid_remarks(adr["remarks"]):
                _fail("ADR remarks type is invalid.")
            if "placards" in adr:
                if not isinstance(adr["placards"], list):
                    _fail("ADR placards must be a list.")
                for placard in adr["placards"]:
                    _validate_placard(placard, assets)
        eri = good.get("eri")
        if eri not in (None, []) and (
            not isinstance(eri, list)
            or any(not isinstance(code, str) or code not in cards for code in eri)
        ):
            _fail("ERI references must point to existing ERI cards.")
    defaults = _mapping(root.get("eri_defaults"), "eri_defaults")
    if any(
        not isinstance(un, str)
        or not _UN.fullmatch(un)
        or not isinstance(code, str)
        or code not in cards
        for un, code in defaults.items()
    ):
        _fail("ERI defaults must reference valid UN numbers and cards.")
    for code, entries in cards.items():
        if not isinstance(code, str) or not isinstance(entries, list) or not entries:
            _fail("ERI cards must be non-empty lists.")
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or entry[0] not in {"title", "heading", "item"}
                or not isinstance(entry[1], str)
            ):
                _fail("ERI card entries must be [kind, text] pairs.")
            if index == 0 and entry[0] != "title":
                _fail("ERI cards must start with a title.")
    sources = root.get("sources")
    if not isinstance(sources, list):
        _fail("Sources must be a list.")
    source_by_id = {source.get("id"): source for source in sources if isinstance(source, dict)}
    for source_id in ("bam", "ericards"):
        source = source_by_id.get(source_id)
        if not isinstance(source, dict) or not isinstance(source.get("legal"), dict):
            _fail("Required BAM and ERICards provenance/legal structures are missing.")
        if not all(
            isinstance(source.get(key), str) and source[key]
            for key in ("provider", "dataset", "source_file", "sha256", "source_url")
        ):
            _fail("Required source provenance is incomplete.")
        legal = source["legal"]
        if source_id == "bam" and not (
            isinstance(legal.get("legal_url"), str)
            and isinstance(legal.get("license"), dict)
            and isinstance(legal.get("attribution"), dict)
            and isinstance(legal.get("processing"), dict)
        ):
            _fail("BAM legal source structure is incomplete.")
        if source_id == "ericards" and not (
            all(
                isinstance(legal.get(key), str)
                for key in ("terms_url", "guidance_url", "disclaimer_url")
            )
            and isinstance(legal.get("attribution"), dict)
            and isinstance(legal.get("reproduction"), dict)
        ):
            _fail("ERICards legal source structure is incomplete.")
    return root, {"goods_count": len(goods), "eri_card_count": len(cards)}
