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
    category: str
    latitude: float | None
    longitude: float | None
    action: str
    pdf_bytes: bytes | None


FIRE_PLAN_COLUMNS = frozenset(
    {
        "external_id",
        "filename",
        "object_name",
        "street_address",
        "postal_code",
        "city",
        "latitude",
        "longitude",
        "action",
    }
)
KLGV_COLUMNS = frozenset({"external_id", "filename", "title", "category", "action"})


def parse_pdf_package(*, payload: bytes, domain: str) -> list[PdfPackageEntry]:
    if len(payload) > settings.MAX_PDF_PACKAGE_BYTES:
        raise ImportValidationError("PDF package exceeds the configured size limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ImportValidationError("PDF package is not a valid ZIP archive.") from error
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > settings.MAX_PDF_PACKAGE_MEMBERS:
            raise ImportValidationError("PDF package member limit exceeded.")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ImportValidationError("PDF package contains duplicate members.")
        if sum(info.file_size for info in infos) > settings.MAX_PDF_PACKAGE_EXPANDED_BYTES:
            raise ImportValidationError("PDF package expanded size limit exceeded.")
        for info in infos:
            _safe_member(info)
        if names.count("manifest.csv") != 1:
            raise ImportValidationError("PDF package requires exactly one manifest.csv.")
        expected_columns = FIRE_PLAN_COLUMNS if domain == "fire_plans" else KLGV_COLUMNS
        rows = _read_manifest(archive.read("manifest.csv"), expected_columns)
        entries = []
        seen_ids: set[str] = set()
        declared: set[str] = set()
        for index, row in enumerate(rows, 2):
            external_id = _required(row, "external_id", index)
            if external_id in seen_ids:
                raise ImportValidationError("PDF package has duplicate external_id values.")
            seen_ids.add(external_id)
            action = row["action"] or "upsert"
            if action not in {"upsert", "deactivate"}:
                raise ImportValidationError(f"Row {index}: invalid action.")
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
            if domain == "fire_plans":
                latitude = _optional_coordinate(row["latitude"], -90, 90, index)
                longitude = _optional_coordinate(row["longitude"], -180, 180, index)
                if (latitude is None) != (longitude is None):
                    raise ImportValidationError("Latitude and longitude must be supplied together.")
                entries.append(
                    PdfPackageEntry(
                        external_identifier=external_id,
                        filename=filename,
                        title=_required(row, "object_name", index) if action == "upsert" else "",
                        address=(
                            _required(row, "street_address", index) if action == "upsert" else ""
                        ),
                        postal_code=row["postal_code"],
                        city=row["city"],
                        category="",
                        latitude=latitude,
                        longitude=longitude,
                        action=action,
                        pdf_bytes=pdf,
                    )
                )
            else:
                entries.append(
                    PdfPackageEntry(
                        external_identifier=external_id,
                        filename=filename,
                        title=_required(row, "title", index) if action == "upsert" else "",
                        address="",
                        postal_code="",
                        city="",
                        category=row["category"],
                        latitude=None,
                        longitude=None,
                        action=action,
                        pdf_bytes=pdf,
                    )
                )
        undeclared = set(names) - {"manifest.csv"} - declared
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
    if len(rows) > settings.MAX_PDF_PACKAGE_MEMBERS - 1:
        raise ImportValidationError("PDF package document limit exceeded.")
    return rows


def _required(row: dict[str, str], field: str, number: int) -> str:
    value = row[field].strip()
    if not value or len(value) > 255:
        raise ImportValidationError(f"Row {number}: {field} is required.")
    return value


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
