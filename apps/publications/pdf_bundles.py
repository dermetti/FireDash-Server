"""Reusable, deterministic schema-v1 bundles of already accepted PDF documents.

This module deliberately consumes only files beneath ``REFERENCE_DATA_ACCEPTED_ROOT``.
Upload/quarantine/sanitization belongs to reference-data services; a publication
builder must never become a second, weaker PDF intake path.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

PDF_BUNDLE_SCHEMA_VERSION = 1
MAX_PDF_BUNDLE_DOCUMENTS = 1_000
MAX_PDF_BUNDLE_MEMBERS = MAX_PDF_BUNDLE_DOCUMENTS + 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PdfBundleError(ValueError):
    """The accepted-document bundle cannot safely be built or validated."""


@dataclass(frozen=True)
class AcceptedPdfBundleDocument:
    """Metadata for one PDF which has already passed FireDash's PDF safety path."""

    id: uuid.UUID
    title: str
    document_key: str
    sha256: str
    page_count: int
    category: str | None = None


def document_archive_path(document_id: uuid.UUID) -> str:
    return f"documents/{document_id}.pdf"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _safe_document_key(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.name != value:
        raise PdfBundleError("Accepted PDF document key is unsafe.")
    return path


def read_accepted_pdf(*, document_key: str, accepted_root: Path) -> bytes:
    safe_key = _safe_document_key(document_key)
    root = accepted_root.resolve()
    path = (root / safe_key).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise PdfBundleError("Accepted PDF document path escapes the accepted root.") from error
    try:
        return path.read_bytes()
    except OSError as error:
        raise PdfBundleError("Accepted PDF document is unavailable.") from error


def _document_entry(document: AcceptedPdfBundleDocument, pdf: bytes) -> dict[str, object]:
    if not isinstance(document.id, uuid.UUID):
        raise PdfBundleError("PDF bundle document ID must be a UUID.")
    title = document.title.strip()
    if not title or len(title) > 255:
        raise PdfBundleError("PDF bundle document title is invalid.")
    if not _SHA256_RE.fullmatch(document.sha256):
        raise PdfBundleError("PDF bundle document SHA-256 is invalid.")
    if type(document.page_count) is not int or document.page_count <= 0:
        raise PdfBundleError("PDF bundle document page count is invalid.")
    if hashlib.sha256(pdf).hexdigest() != document.sha256:
        raise PdfBundleError("Accepted PDF document hash does not match metadata.")
    entry: dict[str, object] = {
        "id": str(document.id),
        "title": title,
        "path": document_archive_path(document.id),
        "sha256": document.sha256,
        "page_count": document.page_count,
    }
    if document.category is not None:
        category = document.category.strip()
        if not category or len(category) > 128:
            raise PdfBundleError("PDF bundle document category is invalid.")
        entry["category"] = category
    return entry


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    return info


def build_pdf_bundle_v1(
    *,
    documents: list[AcceptedPdfBundleDocument],
    source_revision: int,
    accepted_root: Path | None = None,
) -> bytes:
    """Build and self-validate one canonical ZIP of already accepted PDFs."""
    if type(source_revision) is not int or source_revision < 0:
        raise PdfBundleError("PDF bundle source revision is invalid.")
    if len(documents) > MAX_PDF_BUNDLE_DOCUMENTS:
        raise PdfBundleError("PDF bundle has too many documents.")
    root = accepted_root or settings.REFERENCE_DATA_ACCEPTED_ROOT
    ordered = sorted(documents, key=lambda document: str(document.id))
    if len({document.id for document in ordered}) != len(ordered):
        raise PdfBundleError("PDF bundle has duplicate document IDs.")

    entries: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    for document in ordered:
        pdf = read_accepted_pdf(document_key=document.document_key, accepted_root=root)
        entry = _document_entry(document, pdf)
        entries.append(entry)
        payloads.append((str(entry["path"]), pdf))

    manifest = {
        "schema_version": PDF_BUNDLE_SCHEMA_VERSION,
        "source_revision": source_revision,
        "documents": entries,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_zip_info("manifest.json"), _json_bytes(manifest))
        for archive_path, pdf in payloads:
            archive.writestr(_zip_info(archive_path), pdf)
    bundle = output.getvalue()
    validate_pdf_bundle_v1(bundle)
    return bundle


def _safe_zip_name(name: str) -> None:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise PdfBundleError("PDF bundle ZIP contains an unsafe path.")


def validate_pdf_bundle_v1(bundle: bytes) -> dict[str, Any]:
    """Strictly validate the v1 plaintext ZIP before it is encrypted."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise PdfBundleError("PDF bundle is not a valid ZIP archive.") from error
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_PDF_BUNDLE_MEMBERS:
            raise PdfBundleError("PDF bundle member count is invalid.")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise PdfBundleError("PDF bundle contains duplicate ZIP members.")
        for info in infos:
            _safe_zip_name(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise PdfBundleError("PDF bundle ZIP must not contain symbolic links.")
        if "manifest.json" not in names:
            raise PdfBundleError("PDF bundle ZIP is missing manifest.json.")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PdfBundleError("PDF bundle manifest is invalid JSON.") from error
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "source_revision",
            "documents",
        }:
            raise PdfBundleError("PDF bundle manifest schema is invalid.")
        if manifest["schema_version"] != PDF_BUNDLE_SCHEMA_VERSION:
            raise PdfBundleError("PDF bundle manifest schema version is unsupported.")
        if type(manifest["source_revision"]) is not int or manifest["source_revision"] < 0:
            raise PdfBundleError("PDF bundle manifest source revision is invalid.")
        documents = manifest["documents"]
        if not isinstance(documents, list) or len(documents) > MAX_PDF_BUNDLE_DOCUMENTS:
            raise PdfBundleError("PDF bundle manifest documents are invalid.")

        declared_paths: set[str] = set()
        declared_ids: set[uuid.UUID] = set()
        for document in documents:
            if not isinstance(document, dict) or not {
                "id",
                "title",
                "path",
                "sha256",
                "page_count",
            }.issubset(document):
                raise PdfBundleError("PDF bundle document entry is invalid.")
            if set(document) - {"id", "title", "path", "sha256", "page_count", "category"}:
                raise PdfBundleError("PDF bundle document entry has unsupported fields.")
            try:
                document_id = uuid.UUID(str(document["id"]))
            except (ValueError, TypeError, AttributeError) as error:
                raise PdfBundleError("PDF bundle document ID is invalid.") from error
            if str(document_id) != document["id"]:
                raise PdfBundleError("PDF bundle document ID must be canonical lowercase UUID.")
            expected_path = document_archive_path(document_id)
            if document["path"] != expected_path:
                raise PdfBundleError("PDF bundle document path does not match its ID.")
            if document["path"] not in names or document["path"] in declared_paths:
                raise PdfBundleError("PDF bundle document path is missing or duplicated.")
            if document_id in declared_ids:
                raise PdfBundleError("PDF bundle has duplicate document IDs.")
            if (
                not isinstance(document["title"], str)
                or not document["title"]
                or len(document["title"]) > 255
            ):
                raise PdfBundleError("PDF bundle document title is invalid.")
            if not isinstance(document["sha256"], str) or not _SHA256_RE.fullmatch(
                document["sha256"]
            ):
                raise PdfBundleError("PDF bundle document SHA-256 is invalid.")
            if type(document["page_count"]) is not int or document["page_count"] <= 0:
                raise PdfBundleError("PDF bundle document page count is invalid.")
            if "category" in document and (
                not isinstance(document["category"], str)
                or not document["category"]
                or len(document["category"]) > 128
            ):
                raise PdfBundleError("PDF bundle document category is invalid.")
            if hashlib.sha256(archive.read(document["path"])).hexdigest() != document["sha256"]:
                raise PdfBundleError("PDF bundle document hash does not match its bytes.")
            declared_paths.add(document["path"])
            declared_ids.add(document_id)

        if set(names) != {"manifest.json", *declared_paths}:
            raise PdfBundleError("PDF bundle has undeclared members.")
        return manifest
