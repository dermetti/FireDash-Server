import runpy

import pytest

from config.settings.env import EnvironmentConfigurationError, get_bool


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

    with pytest.raises(RuntimeError, match="DJANGO_SECRET_KEY"):
        runpy.run_module("config.settings.production", run_name="production_settings_test")
