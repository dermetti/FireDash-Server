import base64
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from apps.publications.manifests import canonical_manifest_payload, manifest_response_etag

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "complete_manifest_contract.json"


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_complete_manifest_contract_freezes_canonical_signature_bytes_and_etag() -> None:
    fixture = _fixture()
    manifest = fixture["wire_manifest"]
    assert isinstance(manifest, dict)
    assert set(manifest) == {
        "authorization_valid_until",
        "configuration",
        "datasets",
        "generated_at",
        "manifest_generation",
        "signature",
        "signature_algorithm",
        "signing_key_version",
    }
    dataset = manifest["datasets"]
    assert isinstance(dataset, list) and len(dataset) == 1
    assert {
        "publication_id",
        "type",
        "scope",
        "version",
        "schema_version",
        "required",
        "minimum_app_version",
        "artifact_format",
        "encrypted_size",
        "ciphertext_sha256",
        "content_encryption_algorithm",
        "content_encryption_nonce",
        "content_key_wrapped_for_kek",
        "content_key_wrapping_algorithm",
        "content_key_kek_version",
        "artifact_signature",
        "artifact_signature_algorithm",
        "artifact_signing_key_version",
        "download_url",
        "key_grant",
    } == set(dataset[0])

    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    canonical = canonical_manifest_payload(unsigned)
    canonical_ascii = fixture["unsigned_canonical_manifest_ascii"]
    canonical_digest = fixture["unsigned_canonical_manifest_sha256"]
    signature_b64 = fixture["signature"]
    public_key_b64 = fixture["public_key"]
    expected_etag = fixture["expected_manifest_etag"]
    assert isinstance(canonical_ascii, str)
    assert isinstance(canonical_digest, str)
    assert isinstance(signature_b64, str)
    assert isinstance(public_key_b64, str)
    assert isinstance(expected_etag, str)
    assert canonical == canonical_ascii.encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == canonical_digest
    assert manifest_response_etag(manifest) == expected_etag

    signature = base64.b64decode(signature_b64, validate=True)
    public_key = base64.b64decode(public_key_b64, validate=True)
    assert len(signature) == 64
    assert len(public_key) == 32
    assert fixture["signing_key_version"] == manifest["signing_key_version"]
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical)

    tampered = json.loads(json.dumps(unsigned))
    tampered["datasets"][0]["version"] = 8
    with pytest.raises(InvalidSignature):
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, canonical_manifest_payload(tampered)
        )


def test_generated_at_is_signed_but_not_part_of_manifest_etag() -> None:
    fixture = _fixture()
    manifest = fixture["wire_manifest"]
    assert isinstance(manifest, dict)
    updated = json.loads(json.dumps(manifest))
    updated["generated_at"] = "2030-01-02T03:04:06+00:00"

    expected_etag = fixture["expected_manifest_etag"]
    signature_b64 = fixture["signature"]
    public_key_b64 = fixture["public_key"]
    assert isinstance(expected_etag, str)
    assert isinstance(signature_b64, str)
    assert isinstance(public_key_b64, str)
    assert manifest_response_etag(updated) == expected_etag
    signature = base64.b64decode(signature_b64, validate=True)
    public_key = base64.b64decode(public_key_b64, validate=True)
    updated_unsigned = {key: value for key, value in updated.items() if key != "signature"}
    with pytest.raises(InvalidSignature):
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, canonical_manifest_payload(updated_unsigned)
        )
