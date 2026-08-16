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


def test_publication_temp_root_must_descend_from_artifact_root() -> None:
    from pathlib import Path

    from config.settings.base import validate_publication_artifact_layout

    validate_publication_artifact_layout(
        root=Path("/var/lib/fire-backend/publications"),
        temp_root=Path("/var/lib/fire-backend/publications/.tmp"),
    )
    with pytest.raises(EnvironmentConfigurationError, match="PUBLICATION_ARTIFACT_TEMP_ROOT"):
        validate_publication_artifact_layout(
            root=Path("/var/lib/fire-backend/publications"),
            temp_root=Path("/var/lib/fire-backend/publications-tmp"),
        )
    with pytest.raises(EnvironmentConfigurationError, match="PUBLICATION_ARTIFACT_TEMP_ROOT"):
        validate_publication_artifact_layout(
            root=Path("/var/lib/fire-backend/publications"),
            temp_root=Path("/var/lib/fire-backend/publications"),
        )


def test_public_origin_must_be_an_https_origin() -> None:
    from config.settings.base import validate_public_origin

    validate_public_origin("https://firedash.example.org")
    with pytest.raises(RuntimeError, match="FIREDASH_PUBLIC_ORIGIN"):
        validate_public_origin("http://firedash.example.org")
    with pytest.raises(RuntimeError, match="FIREDASH_PUBLIC_ORIGIN"):
        validate_public_origin("https://firedash.example.org/api/v1")


def test_publication_credentials_use_the_current_systemd_credential_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from config.settings.base import publication_credential_path

    monkeypatch.delenv("PUBLICATION_KEK_CREDENTIAL_PATH", raising=False)
    monkeypatch.delenv("PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH", raising=False)
    monkeypatch.delenv("PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH", raising=False)

    delivery_directory = tmp_path / "delivery-invocation"
    build_directory = tmp_path / "build-invocation"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(delivery_directory))
    assert (
        publication_credential_path(
            override_name="PUBLICATION_KEK_CREDENTIAL_PATH", credential_name="publication-kek"
        )
        == delivery_directory / "publication-kek"
    )
    assert (
        publication_credential_path(
            override_name="PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH",
            credential_name="publication-signing-key",
        )
        == delivery_directory / "publication-signing-key"
    )

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(build_directory))
    assert (
        publication_credential_path(
            override_name="PUBLICATION_KEK_CREDENTIAL_PATH", credential_name="publication-kek"
        )
        == build_directory / "publication-kek"
    )
    assert (
        publication_credential_path(
            override_name="PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH",
            credential_name="publication-signing-public-key",
        )
        == build_directory / "publication-signing-public-key"
    )


def test_explicit_publication_credential_path_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from config.settings.base import publication_credential_path

    override = tmp_path / "test-kek"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "systemd"))
    monkeypatch.setenv("PUBLICATION_KEK_CREDENTIAL_PATH", str(override))
    assert (
        publication_credential_path(
            override_name="PUBLICATION_KEK_CREDENTIAL_PATH", credential_name="publication-kek"
        )
        == override
    )


def test_production_settings_reject_temp_root_outside_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.invalid")
    monkeypatch.setenv("PUBLICATION_ARTIFACT_ROOT", "/var/lib/fire-backend/publications")
    monkeypatch.setenv("PUBLICATION_ARTIFACT_TEMP_ROOT", "/var/lib/fire-backend/publications-tmp")

    import runpy

    with _pristine_settings_import():
        with pytest.raises(RuntimeError, match="PUBLICATION_ARTIFACT_TEMP_ROOT"):
            runpy.run_module("config.settings.production", run_name="publication_layout_test")
