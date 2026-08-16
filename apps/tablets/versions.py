"""Stable FireDash application-version parsing and comparison."""

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_MAX_COMPONENT = 2_147_483_647
_MAX_BUILD = 9_223_372_036_854_775_807


class AppVersionError(ValueError):
    """Raised when FireDash application telemetry is not canonical."""


@dataclass(frozen=True, order=True)
class AppVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_app_version(value: str) -> AppVersion:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise AppVersionError("App version must be MAJOR.MINOR.PATCH.")
    parts = tuple(int(part) for part in match.groups())
    if any(part > _MAX_COMPONENT for part in parts):
        raise AppVersionError("App version component is too large.")
    return AppVersion(*parts)


def validate_app_version(value: str) -> None:
    parse_app_version(value)


def parse_app_build(value: str | int) -> int:
    try:
        build = int(value)
    except (TypeError, ValueError) as error:
        raise AppVersionError("App build must be a positive integer.") from error
    if str(value) != str(build) or build < 1 or build > _MAX_BUILD:
        raise AppVersionError("App build must be a positive integer.")
    return build
