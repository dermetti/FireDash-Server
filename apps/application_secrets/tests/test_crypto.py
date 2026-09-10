import base64
import json

import pytest
from django.test import override_settings

from apps.application_secrets.crypto import (
    ApplicationSecretCipher,
    ApplicationSecretError,
    decrypt_application_secret,
    encrypt_application_secret,
    load_application_secret_keyring,
)


def _keyring(path, *, keys: dict[str, bytes]) -> None:
    path.write_text(
        json.dumps(
            {"keys": {version: base64.b64encode(key).decode() for version, key in keys.items()}}
        )
    )


def test_application_secret_round_trip_is_random_and_redacted(tmp_path) -> None:
    keyring = tmp_path / "application-secret-kek-ring"
    _keyring(keyring, keys={"1": b"k" * 32})
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring,
        APPLICATION_SECRET_KEK_VERSION="1",
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "unavailable-publication-kek",
    ):
        first = encrypt_application_secret("smtp-password", context="mail:department:7")
        second = encrypt_application_secret("smtp-password", context="mail:department:7")
        assert first.serialized != second.serialized
        assert "smtp-password" not in first.serialized
        assert str(first) == "EncryptedApplicationSecret(<redacted>)"
        assert "smtp-password" not in repr(first)
        assert decrypt_application_secret(first, context="mail:department:7") == b"smtp-password"


@pytest.mark.parametrize("mutation", ["tamper", "wrong_context", "wrong_key"])
def test_authenticated_failures_are_controlled(mutation) -> None:
    cipher = ApplicationSecretCipher(keys={"1": b"k" * 32}, active_version="1")
    encrypted = cipher.encrypt("not-for-errors", context="mail")
    target = (
        encrypted.serialized[:-1] + ("A" if encrypted.serialized[-1] != "A" else "B")
        if mutation == "tamper"
        else encrypted
    )
    context = "wrong-mail" if mutation == "wrong_context" else "mail"
    if mutation == "wrong_key":
        cipher = ApplicationSecretCipher(keys={"1": b"x" * 32}, active_version="1")
    with pytest.raises(ApplicationSecretError) as error:
        cipher.decrypt(target, context=context)
    assert error.value.code == "decryption_failed"
    assert "not-for-errors" not in str(error.value)


@pytest.mark.parametrize(
    "serialized,code",
    [
        ("not-an-envelope", "malformed_envelope"),
        ("app-secret:v1:2:AAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAA", "unknown_key_version"),
    ],
)
def test_malformed_and_unknown_key_versions_fail_closed(serialized, code) -> None:
    cipher = ApplicationSecretCipher(keys={"1": b"k" * 32}, active_version="1")
    with pytest.raises(ApplicationSecretError) as error:
        cipher.decrypt(serialized, context="mail")
    assert error.value.code == code


def test_credential_file_loading_and_key_rotation(tmp_path) -> None:
    keyring = tmp_path / "application-secret-kek-ring"
    _keyring(keyring, keys={"1": b"a" * 32, "2": b"b" * 32})
    old = load_application_secret_keyring(keyring, active_version="1").encrypt(
        "secret", context="mail"
    )
    rotated = load_application_secret_keyring(keyring, active_version="2")
    assert rotated.decrypt(old, context="mail") == b"secret"
    assert rotated.encrypt("secret", context="mail").serialized.split(":")[2] == "2"


def test_missing_configured_active_key_version_fails_closed(tmp_path) -> None:
    keyring = tmp_path / "application-secret-kek-ring"
    _keyring(keyring, keys={"1": b"a" * 32})
    with pytest.raises(ApplicationSecretError) as error:
        load_application_secret_keyring(keyring, active_version="2")
    assert error.value.code == "invalid_key_configuration"


@pytest.mark.parametrize("contents", [b"", b"{}", b'{"keys":{"1":"d3Jvbmc="}}'])
def test_missing_or_invalid_credential_file_fails_closed(tmp_path, contents) -> None:
    keyring = tmp_path / "application-secret-kek-ring"
    if contents:
        keyring.write_bytes(contents)
    with pytest.raises(ApplicationSecretError) as error:
        load_application_secret_keyring(keyring, active_version="1")
    assert error.value.code in {"key_unavailable", "invalid_key_configuration"}
