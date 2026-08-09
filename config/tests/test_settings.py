import sys
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from config.settings.env import EnvironmentConfigurationError, get_bool


@contextmanager
def _pristine_settings_import() -> Generator[None]:
    saved = {}
    for module_name in list(sys.modules):
        if module_name.startswith("config.settings"):
            saved[module_name] = sys.modules.pop(module_name)
    try:
        yield
    finally:
        sys.modules.update(saved)


def test_invalid_boolean_environment_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDATION_BOOLEAN", "sometimes")

    with pytest.raises(EnvironmentConfigurationError):
        get_bool("FOUNDATION_BOOLEAN")


def test_production_settings_require_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "DJANGO_SECRET_KEY",
        "DJANGO_ALLOWED_HOSTS",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
    ):
        monkeypatch.delenv(variable, raising=False)

    import runpy

    with _pristine_settings_import():
        with pytest.raises(RuntimeError, match="DJANGO_SECRET_KEY"):
            runpy.run_module("config.settings.production", run_name="production_settings_test")
