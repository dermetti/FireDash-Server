import base64
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import override_settings

from apps.publications import artifacts
from apps.publications.artifacts import (
    ArtifactError,
    _signature_payload,
    build_encrypted_artifact,
    cleanup_stale_artifacts,
    remove_artifact_path,
)


@pytest.mark.parametrize("encoded", [False, True])
def test_artifact_is_aes_gcm_wrapped_and_signed(tmp_path, encoded, monkeypatch):
    monkeypatch.setattr(
        artifacts,
        "_set_final_artifact_permissions",
        lambda path: None,
    )
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
        PUBLICATION_SIGNING_KEY_VERSION="43",
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
    assert metadata["artifact_signing_key_version"] == "43"
    cek = keywrap.aes_key_unwrap(kek, wrapped_cek)
    assert AESGCM(cek).decrypt(nonce, ciphertext, None) == b"safe data"


def test_final_artifact_is_group_readable_by_nginx_reader(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"ciphertext")
    chown_calls, chmod_calls = [], []

    monkeypatch.setattr(
        artifacts, "grp", SimpleNamespace(getgrnam=lambda _: SimpleNamespace(gr_gid=4321))
    )
    monkeypatch.setattr(
        artifacts.os, "chown", lambda *args: chown_calls.append(args), raising=False
    )
    monkeypatch.setattr(artifacts.os, "chmod", lambda *args: chmod_calls.append(args))

    artifacts._set_final_artifact_permissions(artifact)

    assert chown_calls == [(artifact, -1, 4321)]
    assert chmod_calls == [(artifact, 0o640)]


def test_artifact_signature_contract_fixture_is_canonical_and_verifiable():
    fixture_path = Path(__file__).parent / "fixtures" / "artifact_signature_contract.json"
    fixture = json.loads(fixture_path.read_text(encoding="ascii"))
    publication = SimpleNamespace(**fixture["publication"])
    wrapped_cek = base64.b64decode(fixture["wrapped_cek"], validate=True)
    nonce = base64.b64decode(fixture["nonce"], validate=True)
    ciphertext = base64.b64decode(fixture["ciphertext"], validate=True)

    with override_settings(PUBLICATION_KEK_VERSION="42"):
        payload = _signature_payload(
            publication=publication,
            wrapped_cek=wrapped_cek,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    assert payload == fixture["canonical_payload"].encode("ascii")
    assert b'"publication_id"' not in payload
    public_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(fixture["private_seed"], validate=True)
    ).public_key()
    assert base64.b64encode(public_key.public_bytes_raw()).decode("ascii") == fixture["public_key"]
    public_key.verify(base64.b64decode(fixture["signature"], validate=True), payload)


def test_artifacts_use_distinct_cek_and_nonce(tmp_path, monkeypatch):
    monkeypatch.setattr(
        artifacts,
        "_set_final_artifact_permissions",
        lambda path: None,
    )
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
