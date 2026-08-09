from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.reference_data.storage import StorageError, promote_to_accepted, write_quarantine


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
    accepted = promote_to_accepted(source, "123e4567-e89b-12d3-a456-426614174000.pdf")

    assert accepted.parent == private_roots / "accepted"
    assert accepted.read_bytes() == b"safe"


def test_accepted_storage_rejects_traversal_keys(private_roots: Path) -> None:
    source = private_roots / "source.pdf"
    source.write_bytes(b"safe")

    with pytest.raises(StorageError, match="Invalid generated"):
        promote_to_accepted(source, "../unsafe.pdf")
