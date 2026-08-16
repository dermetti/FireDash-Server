"""Read-only, version-addressable Ed25519 public signing-key ring."""

import base64
import json
import re

from django.conf import settings


class SigningKeyConfigurationError(ValueError):
    """The public signing-key ring is absent or does not meet its contract."""


_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_signing_key_version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise SigningKeyConfigurationError("Publication signing-key version is invalid.")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SigningKeyConfigurationError("Publication signing-key ring has duplicate keys.")
        result[key] = value
    return result


def publication_signing_public_key_ring() -> dict[str, bytes]:
    """Load every configured public key without accessing private credentials."""
    try:
        encoded = settings.PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH.read_bytes()
        document = json.loads(encoded.decode("ascii"), object_pairs_hook=_no_duplicate_keys)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SigningKeyConfigurationError,
    ) as error:
        raise SigningKeyConfigurationError(
            "Publication public signing-key ring is unavailable."
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"keys"}
        or not isinstance(document["keys"], dict)
    ):
        raise SigningKeyConfigurationError("Publication public signing-key ring is invalid.")

    keys: dict[str, bytes] = {}
    for version, public_key_b64 in document["keys"].items():
        version = validate_signing_key_version(version)
        if not isinstance(public_key_b64, str):
            raise SigningKeyConfigurationError("Publication public signing-key ring is invalid.")
        try:
            public_key = base64.b64decode(public_key_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as error:
            raise SigningKeyConfigurationError(
                "Publication public signing-key ring is invalid."
            ) from error
        if len(public_key) != 32:
            raise SigningKeyConfigurationError(
                "Publication Ed25519 public key must be exactly 32 bytes."
            )
        keys[version] = public_key

    active_version = validate_signing_key_version(settings.PUBLICATION_SIGNING_KEY_VERSION)
    if active_version not in keys:
        raise SigningKeyConfigurationError(
            "Active publication signing-key version is absent from the public key ring."
        )
    return keys


def publication_signing_public_key_for_version(version: object) -> bytes:
    """Return only the exact configured public key for ``version``."""
    return publication_signing_public_key_ring()[validate_signing_key_version(version)]


def active_publication_signing_key() -> bytes:
    return publication_signing_public_key_for_version(settings.PUBLICATION_SIGNING_KEY_VERSION)
