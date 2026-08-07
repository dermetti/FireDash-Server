"""Strict, dependency-free environment configuration helpers."""

import os
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class EnvironmentConfigurationError(RuntimeError):
    """Raised when required runtime configuration is unavailable or invalid."""


def get_env(name: str, *, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise EnvironmentConfigurationError(f"Required environment variable {name} is not set.")
    if value is None:
        raise EnvironmentConfigurationError(f"Environment variable {name} has no default value.")
    return value


def get_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise EnvironmentConfigurationError(f"Environment variable {name} must be a boolean.")


def get_list(name: str, *, default: tuple[str, ...] = ()) -> list[str]:
    value = os.environ.get(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def get_typed_env(name: str, converter: Callable[[str], T], *, default: T | None = None) -> T:
    value = os.environ.get(name)
    if value is None:
        if default is None:
            raise EnvironmentConfigurationError(f"Required environment variable {name} is not set.")
        return default
    try:
        return converter(value)
    except (TypeError, ValueError) as error:
        raise EnvironmentConfigurationError(f"Environment variable {name} is invalid.") from error
