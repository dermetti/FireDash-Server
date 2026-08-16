import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import override_settings

from apps.publications.signing_keys import (
    SigningKeyConfigurationError,
    active_publication_signing_key,
    publication_signing_public_key_for_version,
    publication_signing_public_key_ring,
)
from apps.publications.worker_grants import KeyGrantError, sign_manifest_payload


def _public_key(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()


def _ring(path, keys: dict[str, bytes]) -> None:
    path.write_text(
        json.dumps(
            {
                "keys": {
                    version: base64.b64encode(key).decode("ascii") for version, key in keys.items()
                }
            },
            sort_keys=True,
        ),
        encoding="ascii",
    )


def test_public_signing_key_ring_resolves_exact_historical_versions(tmp_path) -> None:
    ring = tmp_path / "ring.json"
    key_v1, key_v2 = _public_key(b"1" * 32), _public_key(b"2" * 32)
    _ring(ring, {"1": key_v1, "2": key_v2})
    with override_settings(
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring,
        PUBLICATION_SIGNING_KEY_VERSION="2",
    ):
        assert publication_signing_public_key_for_version("1") == key_v1
        assert publication_signing_public_key_for_version("2") == key_v2
        assert active_publication_signing_key() == key_v2
        with pytest.raises(KeyError):
            publication_signing_public_key_for_version("unknown")


@pytest.mark.parametrize(
    "document",
    [
        b"not-json",
        b'["keys"]',
        b'{"keys":{"1":"not-base64"}}',
        b'{"keys":{"1":"YQ=="}}',
        b'{"keys":{"1":"YQ==","1":"Yg=="}}',
    ],
)
def test_invalid_public_signing_key_ring_fails_closed(tmp_path, document: bytes) -> None:
    ring = tmp_path / "ring.json"
    ring.write_bytes(document)
    with override_settings(
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring,
        PUBLICATION_SIGNING_KEY_VERSION="1",
    ):
        with pytest.raises(SigningKeyConfigurationError):
            publication_signing_public_key_ring()


def test_active_version_missing_from_public_ring_fails_closed(tmp_path) -> None:
    ring = tmp_path / "ring.json"
    _ring(ring, {"1": _public_key(b"1" * 32)})
    with override_settings(
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring,
        PUBLICATION_SIGNING_KEY_VERSION="2",
    ):
        with pytest.raises(SigningKeyConfigurationError, match="absent"):
            active_publication_signing_key()


def test_worker_refuses_a_private_key_that_does_not_match_the_active_ring(tmp_path) -> None:
    ring = tmp_path / "ring.json"
    private_key = tmp_path / "private"
    _ring(ring, {"1": _public_key(b"1" * 32)})
    private_key.write_bytes(b"2" * 32)
    with override_settings(
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=private_key,
        PUBLICATION_SIGNING_KEY_VERSION="1",
    ):
        with pytest.raises(KeyGrantError, match="does not match"):
            sign_manifest_payload(payload={"manifest_generation": 1, "datasets": []})
