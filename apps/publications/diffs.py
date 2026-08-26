"""Bounded source-snapshot diffs for administrator publication inspection."""

from __future__ import annotations

from typing import Any

PREVIEW_LIMIT = 25


def source_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare the canonical payload used by source_fingerprint, never artifacts."""
    collection_key = next(
        (
            key
            for key in ("features", "people", "fire_plans", "klgv_plans")
            if key in after or key in before
        ),
        None,
    )
    if collection_key is None:
        return {"added": 0, "removed": 0, "changed": 0, "total": 0, "preview": []}
    before_rows = _by_id(before.get(collection_key, []))
    after_rows = _by_id(after.get(collection_key, []))
    preview: list[dict[str, Any]] = []
    added = removed = changed = 0
    for identity in sorted(set(before_rows) | set(after_rows)):
        old, new = before_rows.get(identity), after_rows.get(identity)
        if old is None:
            added += 1
            preview.append({"kind": "Added", "label": _label(new, identity), "fields": []})
        elif new is None:
            removed += 1
            preview.append({"kind": "Removed", "label": _label(old, identity), "fields": []})
        else:
            fields = _changed_fields(old, new)
            if fields:
                changed += 1
                preview.append(
                    {"kind": "Changed", "label": _label(new, identity), "fields": fields}
                )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "total": added + removed + changed,
        "preview": preview[:PREVIEW_LIMIT],
        "truncated": len(preview) > PREVIEW_LIMIT,
    }


def _by_id(rows: Any) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            identity = row.get("id") or row.get("properties", {}).get("external_identifier")
            if identity is not None:
                result[str(identity)] = row
    return result


def _label(row: dict[str, Any] | None, fallback: str) -> str:
    if not row:
        return fallback
    properties = row.get("properties", {}) if isinstance(row.get("properties"), dict) else {}
    return str(
        row.get("display_name")
        or row.get("object_name")
        or row.get("external_identifier")
        or properties.get("external_identifier")
        or fallback
    )


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    old = _flatten(old)
    new = _flatten(new)
    changes = []
    for key in sorted(set(old) | set(new)):
        if key in {"id", "type", "path"}:
            continue
        old_value, new_value = old.get(key), new.get(key)
        if old_value != new_value:
            if key == "sha256":
                # The source manifest uses the accepted plaintext hash as the
                # stable PDF-content identity.  It is comparison evidence, not
                # administrator-facing artifact metadata.
                changes.append(
                    {
                        "field": "PDF content",
                        "before": "Previous content",
                        "after": "Updated content",
                    }
                )
                continue
            changes.append(
                {
                    "field": key.replace("_", " ").capitalize(),
                    "before": old_value,
                    "after": new_value,
                }
            )
    return changes


def _flatten(value: dict[str, Any]) -> dict[str, Any]:
    flattened = dict(value)
    properties = flattened.pop("properties", None)
    if isinstance(properties, dict):
        flattened.update(properties)
    return flattened
