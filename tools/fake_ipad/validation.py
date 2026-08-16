"""Strict response-shape validation helpers shared across protocol steps."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from tools.fake_ipad.errors import fail

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
APP_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def require_uuid(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        fail(f"{label}: expected UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        fail(f"{label}: invalid UUID {value!r}")
    canonical = str(parsed)
    if canonical != value.lower():
        fail(f"{label}: UUID is not canonical: {value!r}")
    return canonical


def require_string(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        fail(f"{label}: expected a non-empty string")
    return value


def require_app_version(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not APP_VERSION_RE.fullmatch(value):
        fail(f"{label}: expected MAJOR.MINOR.PATCH with non-negative numeric components")
    return value


def require_app_build(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        fail(f"{label}: expected a positive integer")
    return value


def require_timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label}: expected RFC 3339 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(f"{label}: invalid RFC 3339 timestamp {value!r}")
    if parsed.tzinfo is None:
        fail(f"{label}: timestamp must include a UTC offset")
    return value


def require_keys(obj: dict[str, Any], keys: Iterable[str], *, label: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        fail(f"{label}: missing fields: {', '.join(missing)}")
