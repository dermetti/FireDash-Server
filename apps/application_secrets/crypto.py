"""Authenticated encryption for application credentials stored outside the KEK.

The serialized envelope is safe to persist, but is intentionally not useful as
a log value: value objects redact it in their normal representations.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

_FORMAT = "app-secret"
_FORMAT_VERSION = "v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ApplicationSecretError(Exception):
    """A controlled failure while loading or opening an application secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Application secret operation failed ({code}).")


@dataclass(frozen=True, repr=False)
class EncryptedApplicationSecret:
    """A persistable encrypted envelope whose representations are redacted."""

    serialized: str

    def __str__(self) -> str:
        return "EncryptedApplicationSecret(<redacted>)"

    def __repr__(self) -> str:
        return "EncryptedApplicationSecret(<redacted>)"


def _as_bytes(value: str | bytes, *, field: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"{field} must be str or bytes")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ApplicationSecretError("malformed_envelope")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):  # type: ignore[name-defined]
        raise ApplicationSecretError("malformed_envelope") from None


def _parse_envelope(serialized: str) -> tuple[str, bytes, bytes]:
    if not isinstance(serialized, str):
        raise ApplicationSecretError("malformed_envelope")
    parts = serialized.split(":")
    if len(parts) != 5 or parts[0] != _FORMAT or parts[1] != _FORMAT_VERSION:
        raise ApplicationSecretError("malformed_envelope")
    version, nonce_text, ciphertext_text = parts[2:]
    if not _VERSION_RE.fullmatch(version):
        raise ApplicationSecretError("malformed_envelope")
    nonce = _b64decode(nonce_text)
    ciphertext = _b64decode(ciphertext_text)
    if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
        raise ApplicationSecretError("malformed_envelope")
    return version, nonce, ciphertext


class ApplicationSecretCipher:
    """Versioned AES-256-GCM cipher using deployment-provided KEKs only."""

    def __init__(self, *, keys: Mapping[str, bytes], active_version: str) -> None:
        if not _VERSION_RE.fullmatch(active_version) or active_version not in keys:
            raise ApplicationSecretError("invalid_key_configuration")
        normalized: dict[str, bytes] = {}
        for version, key in keys.items():
            if (
                not _VERSION_RE.fullmatch(version)
                or not isinstance(key, bytes)
                or len(key) != _KEY_BYTES
            ):
                raise ApplicationSecretError("invalid_key_configuration")
            normalized[version] = key
        self._keys = normalized
        self._active_version = active_version

    def encrypt(
        self, plaintext: str | bytes, *, context: str | bytes
    ) -> EncryptedApplicationSecret:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self._active_version]).encrypt(
            nonce, _as_bytes(plaintext, field="plaintext"), _as_bytes(context, field="context")
        )
        return EncryptedApplicationSecret(
            f"{_FORMAT}:{_FORMAT_VERSION}:{self._active_version}:{_b64encode(nonce)}:{_b64encode(ciphertext)}"
        )

    def decrypt(
        self, encrypted: EncryptedApplicationSecret | str, *, context: str | bytes
    ) -> bytes:
        serialized = (
            encrypted.serialized if isinstance(encrypted, EncryptedApplicationSecret) else encrypted
        )
        version, nonce, ciphertext = _parse_envelope(serialized)
        key = self._keys.get(version)
        if key is None:
            raise ApplicationSecretError("unknown_key_version")
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, _as_bytes(context, field="context"))
        except (InvalidTag, ValueError):
            raise ApplicationSecretError("decryption_failed") from None


def load_application_secret_keyring(path: Path, *, active_version: str) -> ApplicationSecretCipher:
    """Load the root-managed JSON key-ring credential without exposing its bytes."""
    try:
        payload = json.loads(path.read_bytes())
        encoded_keys = payload["keys"]
        if not isinstance(encoded_keys, dict):
            raise ValueError
        keys = {
            version: base64.b64decode(encoded, validate=True)
            for version, encoded in encoded_keys.items()
            if isinstance(version, str) and isinstance(encoded, str)
        }
        if len(keys) != len(encoded_keys):
            raise ValueError
        return ApplicationSecretCipher(keys=keys, active_version=active_version)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise ApplicationSecretError("key_unavailable") from None


def configured_application_secret_cipher() -> ApplicationSecretCipher:
    """Build a cipher from the dedicated Django application-secret settings."""
    return load_application_secret_keyring(
        Path(settings.APPLICATION_SECRET_KEK_CREDENTIAL_PATH),
        active_version=settings.APPLICATION_SECRET_KEK_VERSION,
    )


def encrypt_application_secret(
    plaintext: str | bytes, *, context: str | bytes
) -> EncryptedApplicationSecret:
    return configured_application_secret_cipher().encrypt(plaintext, context=context)


def decrypt_application_secret(
    encrypted: EncryptedApplicationSecret | str, *, context: str | bytes
) -> bytes:
    return configured_application_secret_cipher().decrypt(encrypted, context=context)
