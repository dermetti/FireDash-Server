import base64
import errno
import hashlib
import json
import logging
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
    monkeypatch.setattr(
        artifacts,
        "_set_final_directory_permissions",
        lambda path: None,
    )
    kek, signing_seed = b"k" * 32, b"s" * 32
    kek_path, signing_path = tmp_path / "kek", tmp_path / "signing"
    kek_path.write_bytes(base64.b64encode(kek) if encoded else kek)
    signing_path.write_bytes(base64.b64encode(signing_seed) if encoded else signing_seed)
    publication = SimpleNamespace(
        id="artifact-id",
        scope_type="DEPARTMENT",
        department_id="department-id",
        station_id=None,
        dataset_type_code="department_hydrants",
        schema_version=1,
        version_number=7,
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "final",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "final" / ".tmp",
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


def test_publication_artifact_relative_path_is_canonical_forward_slash():
    from apps.publications.paths import publication_artifact_relative_path

    path = publication_artifact_relative_path(department_id="dept-id", publication_id="pub-id")
    assert path == "dept-id/pub-id/artifact.bin"
    assert "\\" not in path


def test_final_directory_permissions_apply_group_and_mode(tmp_path, monkeypatch):
    directory = tmp_path / "dept" / "pub"
    directory.mkdir(parents=True)
    chown_calls, chmod_calls = [], []

    monkeypatch.setattr(
        artifacts, "grp", SimpleNamespace(getgrnam=lambda _: SimpleNamespace(gr_gid=4321))
    )
    monkeypatch.setattr(
        artifacts.os, "chown", lambda *args: chown_calls.append(args), raising=False
    )
    monkeypatch.setattr(artifacts.os, "chmod", lambda *args: chmod_calls.append(args))

    artifacts._set_final_directory_permissions(directory)

    assert chown_calls == [(directory, -1, 4321)]
    assert chmod_calls == [(directory, 0o750)]


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
    monkeypatch.setattr(
        artifacts,
        "_set_final_directory_permissions",
        lambda path: None,
    )
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    publication = SimpleNamespace(
        id="artifact-id",
        scope_type="DEPARTMENT",
        department_id="department-id",
        station_id=None,
        dataset_type_code="department_hydrants",
        schema_version=1,
        version_number=1,
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "final",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "final" / ".tmp",
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
    stale = tmp_path / "final" / ".tmp" / "abandoned"
    stale.mkdir(parents=True)
    os.utime(stale, (time.time() - 120, time.time() - 120))
    from apps.publications.models import DatasetPublication

    monkeypatch.setattr(DatasetPublication, "objects", SimpleNamespace(filter=lambda **_: []))
    with override_settings(
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "final" / ".tmp",
        PUBLICATION_ARTIFACT_STALE_SECONDS=60,
    ):
        assert cleanup_stale_artifacts() == 1
    assert not stale.exists()
    assert (tmp_path / "final" / ".tmp").exists()


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


def test_credential_reads_raw_32_bytes_with_whitespace_boundaries(tmp_path):
    key = bytes([0x20]) + b"x" * 30 + bytes([0x0A])
    assert len(key) == 32
    path = tmp_path / "key"
    path.write_bytes(key)
    assert artifacts._credential(path, "test") == key


def test_credential_decodes_base64_text(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(base64.b64encode(b"k" * 32) + b"\n")
    assert artifacts._credential(path, "test") == b"k" * 32


def _publication(**overrides) -> SimpleNamespace:
    values = {
        "scope_type": "DEPARTMENT",
        "id": "artifact-id",
        "department_id": "department-id",
        "station_id": None,
        "dataset_type_code": "department_hydrants",
        "schema_version": 1,
        "version_number": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _artifact_settings(root: Path, tmp_path: Path):
    return override_settings(
        PUBLICATION_ARTIFACT_ROOT=root,
        PUBLICATION_ARTIFACT_TEMP_ROOT=root / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="1",
    )


def test_promotion_uses_atomic_replace_on_the_common_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    replaced = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", spy_replace)
    root = tmp_path / "publications"
    with _artifact_settings(root, tmp_path):
        build_encrypted_artifact(publication=_publication(), plaintext=b"payload")

    assert replaced == [
        (
            root / ".tmp" / "artifact-id" / "artifact.bin",
            root / "department-id" / "artifact-id" / "artifact.bin",
        )
    ]
    assert (root / "department-id" / "artifact-id" / "artifact.bin").exists()


def test_promotion_does_not_fall_back_to_copy_on_exdev(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)

    def failing_replace(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(artifacts.os, "replace", failing_replace)
    root = tmp_path / "publications"
    with _artifact_settings(root, tmp_path):
        with pytest.raises(ArtifactError, match="promote"):
            build_encrypted_artifact(publication=_publication(), plaintext=b"payload")


def test_promotion_failure_logs_oserror_context_without_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)

    def failing_replace(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(artifacts.os, "replace", failing_replace)
    root = tmp_path / "publications"
    with _artifact_settings(root, tmp_path):
        with pytest.raises(ArtifactError):
            with caplog.at_level(logging.ERROR, logger="apps.publications.artifacts"):
                build_encrypted_artifact(
                    publication=_publication(), plaintext=b"super-secret-plaintext"
                )

    assert "Invalid cross-device link" in caplog.text
    assert "super-secret-plaintext" not in caplog.text


def test_temp_directory_is_created_owner_private(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    requested = []
    real_mkdir = os.mkdir

    def spy_mkdir(path, mode=0o777, *args, **kwargs):
        requested.append((Path(path), mode))
        real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", spy_mkdir)
    root = tmp_path / "publications"
    with _artifact_settings(root, tmp_path):
        build_encrypted_artifact(publication=_publication(), plaintext=b"payload")

    temp_dir = root / ".tmp" / "artifact-id"
    modes = [mode for path, mode in requested if path == temp_dir]
    assert modes
    assert all(mode == 0o700 for mode in modes)
