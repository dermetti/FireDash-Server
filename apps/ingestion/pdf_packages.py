"""Strict ZIP package inspection for canonical PDF imports.

Sanitization remains a separate mandatory step.  This module only establishes
that a package has one unambiguous manifest-to-member mapping before any PDF is
handed to the sanitizer broker.
"""

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.conf import settings

from apps.ingestion.parsers import ImportValidationError


@dataclass(frozen=True)
class PdfPackageEntry:
    external_identifier: str
    filename: str
    title: str
    address: str
    postal_code: str
    city: str
    fsd_location: str
    bmz_location: str
    rwa_info: str
    category: str
    latitude: float | None
    longitude: float | None
    action: str
    pdf_bytes: bytes | None


FIRE_PLAN_COLUMNS = frozenset(
    {
        "external_identifier",
        "filename",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "fsd_location",
        "bmz_location",
        "rwa_info",
        "action",
    }
)
KLGV_COLUMNS = frozenset(
    {
        "external_identifier",
        "filename",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "action",
    }
)

# Canonical ZIP member names. Fire Plans use the versioned manifest name produced
# by the curation tool; KLGV keeps its existing documented ``manifest.csv`` name.
FIRE_PLAN_MANIFEST_NAME = "fire-plans-manifest-v1.csv"
KLGV_MANIFEST_NAME = "manifest.csv"


def manifest_member_name(domain: str) -> str:
    """Return the canonical manifest member name for a PDF package domain."""
    return FIRE_PLAN_MANIFEST_NAME if domain == "fire_plans" else KLGV_MANIFEST_NAME


def parse_pdf_package(*, payload: bytes, domain: str) -> list[PdfPackageEntry]:
    if len(payload) > settings.MAX_INGEST_UPLOAD_BYTES:
        raise ImportValidationError("PDF package exceeds the configured size limit.")
    manifest_name = manifest_member_name(domain)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ImportValidationError("PDF package is not a valid ZIP archive.") from error
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > settings.MAX_PDF_PACKAGE_DOCUMENTS + 1:
            raise ImportValidationError("PDF package member limit exceeded.")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ImportValidationError("PDF package contains duplicate members.")
        if sum(info.file_size for info in infos) > settings.MAX_PDF_PACKAGE_EXPANDED_BYTES:
            raise ImportValidationError("PDF package expanded size limit exceeded.")
        for info in infos:
            _safe_member(info)
        if names.count(manifest_name) != 1:
            raise ImportValidationError(f"PDF package requires exactly one {manifest_name}.")
        expected_columns = FIRE_PLAN_COLUMNS if domain == "fire_plans" else KLGV_COLUMNS
        rows = _read_manifest(archive.read(manifest_name), expected_columns)
        entries = []
        seen_identities: set[tuple[str, str]] = set()
        declared: set[str] = set()
        for index, row in enumerate(rows, 2):
            action = row["action"] or "upsert"
            if action not in {"upsert", "deactivate"}:
                raise ImportValidationError(f"Row {index}: invalid action.")
            if domain == "fire_plans":
                external_id = row["external_identifier"].strip()
                address = row["address"].strip()
                identity = _identity(external_identifier=external_id, address=address, index=index)
            else:
                external_id = row["external_identifier"].strip()
                address = _required(row, "address", index) if action == "upsert" else ""
                identity = (
                    ("external_identifier", external_id)
                    if external_id
                    else ("object_name_address", f"{row['object_name'].strip()}\x00{address}")
                )
            if identity in seen_identities:
                raise ImportValidationError("PDF package has duplicate Fire Plan identities.")
            seen_identities.add(identity)
            filename = row["filename"]
            if action == "deactivate":
                if filename:
                    raise ImportValidationError("Deactivate rows must not reference a PDF.")
                pdf = None
            else:
                if (
                    not filename
                    or filename != PurePosixPath(filename).name
                    or not filename.endswith(".pdf")
                ):
                    raise ImportValidationError(f"Row {index}: filename is invalid.")
                if filename in declared:
                    raise ImportValidationError(
                        "PDF package manifest declares one PDF more than once."
                    )
                declared.add(filename)
                try:
                    pdf = archive.read(filename)
                except KeyError as error:
                    raise ImportValidationError(f"Row {index}: declared PDF is missing.") from error
                if len(pdf) > settings.MAX_PDF_INPUT_BYTES:
                    raise ImportValidationError(
                        f"Row {index}: PDF exceeds the configured size limit."
                    )
            if domain == "fire_plans":
                longitude = _optional_coordinate(row["longitude"], -180, 180, index)
                latitude = _optional_coordinate(row["latitude"], -90, 90, index)
                entries.append(
                    PdfPackageEntry(
                        external_identifier=external_id,
                        filename=filename,
                        title=row["object_name"].strip() if action == "upsert" else "",
                        address=address if action == "upsert" else address,
                        postal_code=row["postal_code"],
                        city=row["city"],
                        fsd_location=row["fsd_location"],
                        bmz_location=row["bmz_location"],
                        rwa_info=row["rwa_info"],
                        category="",
                        latitude=latitude,
                        longitude=longitude,
                        action=action,
                        pdf_bytes=pdf,
                    )
                )
            else:
                longitude = _optional_coordinate(row["longitude"], -180, 180, index)
                latitude = _optional_coordinate(row["latitude"], -90, 90, index)
                entries.append(
                    PdfPackageEntry(
                        external_identifier=external_id,
                        filename=filename,
                        title=_required(row, "object_name", index) if action == "upsert" else "",
                        address=address,
                        postal_code=_required(row, "postal_code", index)
                        if action == "upsert"
                        else "",
                        city=_required(row, "city", index) if action == "upsert" else "",
                        fsd_location="",
                        bmz_location="",
                        rwa_info="",
                        category="",
                        latitude=latitude,
                        longitude=longitude,
                        action=action,
                        pdf_bytes=pdf,
                    )
                )
        undeclared = set(names) - {manifest_name} - declared
        if undeclared:
            raise ImportValidationError("PDF package contains undeclared members.")
        return entries


def _safe_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts or "\\" in info.filename or info.is_dir():
        raise ImportValidationError("PDF package contains an unsafe member path.")
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise ImportValidationError("PDF package must not contain symbolic links.")


def _read_manifest(payload: bytes, expected_columns: frozenset[str]) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ImportValidationError("PDF package manifest must be UTF-8.") from error
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or set(reader.fieldnames) != expected_columns:
        raise ImportValidationError(
            "PDF package manifest columns do not match the documented schema."
        )
    rows = list(reader)
    # Early Fire Plan v1 examples omitted empty trailing operational-location
    # cells.  Keep those compact rows readable while normalising them to the
    # canonical manifest shape; new templates always emit every header value.
    if expected_columns == FIRE_PLAN_COLUMNS:
        for row in rows:
            if row.get("action") is None and row.get("fsd_location") in {"upsert", "deactivate"}:
                row["action"] = row["fsd_location"]
                row["fsd_location"] = ""
                row["bmz_location"] = ""
                row["rwa_info"] = ""
    if len(rows) > settings.MAX_PDF_PACKAGE_DOCUMENTS:
        raise ImportValidationError("PDF package document limit exceeded.")
    return rows


def _required(row: dict[str, str], field: str, number: int) -> str:
    value = row[field].strip()
    if not value or len(value) > 255:
        raise ImportValidationError(f"Row {number}: {field} is required.")
    return value


def _identity(*, external_identifier: str, address: str, index: int) -> tuple[str, str]:
    if external_identifier:
        return ("external_identifier", external_identifier)
    if address:
        return ("address", address)
    raise ImportValidationError(
        f"Row {index}: external_identifier or address is required for a Fire Plan."
    )


def _optional_coordinate(value: str, minimum: float, maximum: float, number: int) -> float | None:
    if value == "":
        return None
    try:
        result = float(value)
    except ValueError as error:
        raise ImportValidationError(f"Row {number}: coordinate is invalid.") from error
    if not minimum <= result <= maximum:
        raise ImportValidationError(f"Row {number}: coordinate is out of range.")
    return result
