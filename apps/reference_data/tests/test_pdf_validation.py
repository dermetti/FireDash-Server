from pathlib import Path
from unittest.mock import patch

import pikepdf
import pytest
from django.test import override_settings

from apps.reference_data.pdf_validation import PdfValidationError, validate_pdf


def write_pdf(path: Path, *, javascript: bool = False, encrypted: bool = False) -> None:
    document = pikepdf.Pdf.new()
    document.add_blank_page()
    if javascript:
        document.Root["/OpenAction"] = pikepdf.Dictionary(
            S=pikepdf.Name("/JavaScript"), JS="app.alert('unsafe')"
        )
    if encrypted:
        document.save(path, encryption=pikepdf.Encryption(user="user", owner="owner", R=6))
    else:
        document.save(path)


@patch("apps.reference_data.pdf_validation._validate_mime")
def test_valid_pdf_is_reopened_and_hashed(_validate_mime, tmp_path: Path) -> None:
    path = tmp_path / "safe.pdf"
    write_pdf(path)

    size, page_count, digest = validate_pdf(path, original_filename="safe.pdf")

    assert size > 0
    assert page_count == 1
    assert len(digest) == 64


@patch("apps.reference_data.pdf_validation._validate_mime")
def test_encrypted_and_active_pdfs_are_rejected(_validate_mime, tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.pdf"
    active = tmp_path / "active.pdf"
    write_pdf(encrypted, encrypted=True)
    write_pdf(active, javascript=True)

    with pytest.raises(PdfValidationError, match="Encrypted"):
        validate_pdf(encrypted, original_filename="encrypted.pdf")
    with pytest.raises(PdfValidationError, match="prohibited"):
        validate_pdf(active, original_filename="active.pdf")


@patch("apps.reference_data.pdf_validation._validate_mime")
@override_settings(MAX_PDF_PAGES=0)
def test_pdf_page_limit_is_enforced(_validate_mime, tmp_path: Path) -> None:
    path = tmp_path / "safe.pdf"
    write_pdf(path)

    with pytest.raises(PdfValidationError, match="page limit"):
        validate_pdf(path, original_filename="safe.pdf")
