import base64
import hashlib
import os
import time
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import override_settings

from apps.publications.artifacts import (
    ArtifactError,
    _signature_payload,
    build_encrypted_artifact,
    cleanup_stale_artifacts,
    remove_artifact_path,
)


@pytest.mark.parametrize("encoded", [False, True])
def test_artifact_is_aes_gcm_wrapped_and_signed(tmp_path, encoded):
    kek, signing_seed = b"k" * 32, b"s" * 32
    kek_path, signing_path = tmp_path / "kek", tmp_path / "signing"
    kek_path.write_bytes(base64.b64encode(kek) if encoded else kek)
    signing_path.write_bytes(base64.b64encode(signing_seed) if encoded else signing_seed)
    publication = SimpleNamespace(
        id="artifact-id",
        department_id="department-id",
        station_id=None,
        dataset_type_code="department_hydrants",
        schema_version=1,
        version_number=7,
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "final",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "temp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_KEK_VERSION="42",
    ):
        metadata = build_encrypted_artifact(publication=publication, plaintext=b"safe data")
        signature = cast(bytes, metadata["artifact_signature"])
        wrapped_cek = cast(bytes, metadata["artifact_wrapped_cek"])
        nonce = cast(bytes, metadata["artifact_nonce"])
        ciphertext = (tmp_path / "final" / metadata["artifact_path"]).read_bytes()
        Ed25519PrivateKey.from_private_bytes(signing_seed).public_key().verify(
            signature,
            _signature_payload(
                publication=publication,
                wrapped_cek=wrapped_cek,
                nonce=nonce,
                ciphertext=ciphertext,
            ),
        )
    assert metadata["artifact_size"] == len(ciphertext)
    assert metadata["artifact_sha256"] == hashlib.sha256(ciphertext).hexdigest()
    assert len(nonce) == 12
    cek = keywrap.aes_key_unwrap(kek, wrapped_cek)
    assert AESGCM(cek).decrypt(nonce, ciphertext, None) == b"safe data"


def test_artifacts_use_distinct_cek_and_nonce(tmp_path):
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    publication = SimpleNamespace(
        id="artifact-id",
        department_id="department-id",
        station_id=None,
        dataset_type_code="department_hydrants",
        schema_version=1,
        version_number=1,
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "final",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "temp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="1",
    ):
        first = build_encrypted_artifact(publication=publication, plaintext=b"one")
        publication.id = "artifact-two"
        second = build_encrypted_artifact(publication=publication, plaintext=b"two")
    assert first["artifact_nonce"] != second["artifact_nonce"]
    assert first["artifact_wrapped_cek"] != second["artifact_wrapped_cek"]


def test_cleanup_removes_stale_temp_directories(tmp_path, monkeypatch):
    stale = tmp_path / "temp" / "abandoned"
    stale.mkdir(parents=True)
    os.utime(stale, (time.time() - 120, time.time() - 120))
    from apps.publications.models import DatasetPublication

    monkeypatch.setattr(DatasetPublication, "objects", SimpleNamespace(filter=lambda **_: []))
    with override_settings(
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "temp", PUBLICATION_ARTIFACT_STALE_SECONDS=60
    ):
        assert cleanup_stale_artifacts() == 1
    assert not stale.exists()


def test_remove_artifact_path_removes_promoted_ciphertext(tmp_path):
    artifact = tmp_path / "final" / "department" / "publication" / "artifact.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"ciphertext")

    with override_settings(PUBLICATION_ARTIFACT_ROOT=tmp_path / "final"):
        remove_artifact_path("department/publication/artifact.bin")

    assert not artifact.exists()


def test_remove_artifact_path_rejects_paths_outside_artifact_root(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"ciphertext")

    with override_settings(PUBLICATION_ARTIFACT_ROOT=tmp_path / "final"):
        with pytest.raises(ArtifactError, match="below the artifact root"):
            remove_artifact_path("../outside.bin")

    assert outside.exists()
