import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.reference_data.storage import (
    StorageError,
    output_path,
    promote_to_accepted,
    write_quarantine,
)


@pytest.fixture
def private_roots(tmp_path: Path):
    with override_settings(
        REFERENCE_DATA_QUARANTINE_ROOT=tmp_path / "quarantine",
        REFERENCE_DATA_SANITIZER_OUTPUT_ROOT=tmp_path / "output",
        REFERENCE_DATA_ACCEPTED_ROOT=tmp_path / "accepted",
        MAX_PDF_INPUT_BYTES=8,
    ):
        yield tmp_path


def test_quarantine_enforces_size_limit_and_generated_accepted_keys(private_roots: Path) -> None:
    with pytest.raises(StorageError, match="size limit"):
        write_quarantine(SimpleUploadedFile("plan.pdf", b"123456789"))

    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")
    with patch("apps.reference_data.storage.os.chmod") as chmod:
        accepted = promote_to_accepted(source, "123e4567-e89b-12d3-a456-426614174000.pdf")

    assert accepted.parent == private_roots / "accepted"
    assert accepted.read_bytes() == b"safe"
    chmod.assert_called_once_with(source, 0o640)


def test_accepted_promotion_does_not_replace_after_mode_failure(
    private_roots: Path, monkeypatch
) -> None:
    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")
    destination = private_roots / "accepted" / "123e4567-e89b-12d3-a456-426614174000.pdf"

    monkeypatch.setattr(
        "apps.reference_data.storage.os.chmod", lambda *_: (_ for _ in ()).throw(OSError())
    )

    with pytest.raises(OSError):
        promote_to_accepted(source, destination.name)

    assert source.exists()
    assert not destination.exists()


def test_accepted_promotion_assigns_configured_reader_group(private_roots: Path) -> None:
    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")
    with override_settings(REFERENCE_DATA_ACCEPTED_GROUP="fire_document_readers"):
        with patch(
            "apps.reference_data.storage.grp",
            SimpleNamespace(getgrnam=lambda _name: SimpleNamespace(gr_gid=4242)),
        ):
            with patch("apps.reference_data.storage.os.chown", create=True) as chown:
                accepted = promote_to_accepted(
                    source, "123e4567-e89b-12d3-a456-426614174000.pdf"
                )

    assert accepted.read_bytes() == b"safe"
    chown.assert_called_once_with(accepted, -1, 4242)


def test_accepted_promotion_refuses_missing_configured_reader_group(private_roots: Path) -> None:
    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")
    with override_settings(REFERENCE_DATA_ACCEPTED_GROUP="fire_document_readers"):
        with patch(
            "apps.reference_data.storage.grp",
            SimpleNamespace(getgrnam=lambda _name: (_ for _ in ()).throw(KeyError)),
        ):
            with pytest.raises(StorageError, match="reader group"):
                promote_to_accepted(source, "123e4567-e89b-12d3-a456-426614174000.pdf")

    assert source.exists()


def test_accepted_storage_rejects_traversal_keys(private_roots: Path) -> None:
    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")

    with pytest.raises(StorageError, match="Invalid generated"):
        promote_to_accepted(source, "../unsafe.pdf")


def test_quarantine_creation_requests_no_sgid(private_roots: Path, monkeypatch) -> None:
    requested: list[tuple[Path, int]] = []
    real_mkdir = Path.mkdir

    def spy_mkdir(self: Path, *args, **kwargs) -> None:
        requested.append((self, kwargs.get("mode", 0o777)))
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)

    chmod_calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def spy_chmod(path: Path, mode: int) -> None:
        chmod_calls.append((Path(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr("apps.reference_data.storage.os.chmod", spy_chmod)

    uploaded = write_quarantine(SimpleUploadedFile("plan.pdf", b"%PDF-1.4"))

    job_directory = uploaded.parent
    assert [mode for path, mode in requested if path == job_directory] == [0o700]
    assert [mode for path, mode in chmod_calls if path == job_directory] == []
    assert [mode for path, mode in chmod_calls if path == uploaded] == [0o640]
    assert uploaded.read_bytes() == b"%PDF-1.4"


def test_output_creation_requests_no_sgid(private_roots: Path, monkeypatch) -> None:
    requested: list[tuple[Path, int]] = []
    real_mkdir = Path.mkdir

    def spy_mkdir(self: Path, *args, **kwargs) -> None:
        requested.append((self, kwargs.get("mode", 0o777)))
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)

    chmod_calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def spy_chmod(path: Path, mode: int) -> None:
        chmod_calls.append((Path(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr("apps.reference_data.storage.os.chmod", spy_chmod)

    sanitized = output_path()

    job_directory = sanitized.parent
    assert [mode for path, mode in requested if path == job_directory] == [0o700]
    assert [mode for path, mode in chmod_calls if path == job_directory] == [0o730]
    assert all(mode & 0o6000 == 0 for _, mode in chmod_calls)


def test_output_path_shares_quarantine_job_id(private_roots: Path) -> None:
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    sanitized = output_path(job_id=job_id)

    assert sanitized.parent.name == job_id
    assert sanitized.name == "sanitized.pdf"


def test_runtime_directories_are_restrict_suidsgid_compatible(
    private_roots: Path, monkeypatch
) -> None:
    real_mkdir = Path.mkdir

    def strict_mkdir(self: Path, *args, **kwargs) -> None:
        if kwargs.get("mode", 0o777) & 0o6000:
            raise PermissionError(1, "Operation not permitted")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", strict_mkdir)

    real_chmod = os.chmod

    def strict_chmod(path: Path, mode: int) -> None:
        if mode & 0o6000:
            raise PermissionError(1, "Operation not permitted")
        real_chmod(path, mode)

    monkeypatch.setattr("apps.reference_data.storage.os.chmod", strict_chmod)

    uploaded = write_quarantine(SimpleUploadedFile("plan.pdf", b"%PDF-1.4"))
    sanitized = output_path()

    assert uploaded.read_bytes() == b"%PDF-1.4"
    assert sanitized.name == "sanitized.pdf"
