import io
import zipfile

import pytest

from apps.ingestion.parsers import ImportValidationError
from apps.ingestion.pdf_packages import parse_pdf_package


def package(manifest: str, **files: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.csv", manifest)
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def fire_manifest(rows: str) -> str:
    return (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
        + rows
    )


def test_pdf_package_has_exact_member_identity_mapping():
    entries = parse_pdf_package(
        payload=package(
            fire_manifest("FP-1,plan.pdf,School,Main 1,12345,Town,8.2,50.1,upsert\n"),
            **{"plan.pdf": b"%PDF-1.4\n"},
        ),
        domain="fire_plans",
    )
    assert entries[0].external_identifier == "FP-1"
    assert entries[0].pdf_bytes == b"%PDF-1.4\n"
    assert (entries[0].longitude, entries[0].latitude) == (8.2, 50.1)


@pytest.mark.parametrize(
    "row",
    [
        ",plan.pdf,,Wandsbeker Zollstraße 95,,,,,upsert\n",
        "FP-1,plan.pdf,,,,,,,upsert\n",
    ],
)
def test_pdf_package_accepts_external_or_address_identity(row):
    entries = parse_pdf_package(
        payload=package(fire_manifest(row), **{"plan.pdf": b"%PDF-1.4\n"}),
        domain="fire_plans",
    )
    assert len(entries) == 1


@pytest.mark.parametrize(
    "row",
    [
        ",plan.pdf,,,,,,,upsert\n",
    ],
)
def test_pdf_package_rejects_missing_or_invalid_identity(row):
    with pytest.raises(ImportValidationError):
        parse_pdf_package(
            payload=package(fire_manifest(row), **{"plan.pdf": b"%PDF-1.4\n"}),
            domain="fire_plans",
        )


@pytest.mark.parametrize("stale_header", ["object_reference", "external_id", "street_address"])
def test_pdf_package_rejects_stale_manifest_headers(stale_header):
    header = (
        "external_identifier,filename,object_name,address,postal_code,city,"
        "longitude,latitude,action"
    )
    header = header.replace("object_name", stale_header)
    with pytest.raises(ImportValidationError):
        parse_pdf_package(
            payload=package(
                f"{header}\nFP-1,plan.pdf,Plan,Road,,,,,upsert\n",
                **{"plan.pdf": b"%PDF-1.4\n"},
            ),
            domain="fire_plans",
        )


@pytest.mark.parametrize(
    "coordinate_row",
    [
        "FP-1,plan.pdf,Plan,Road,,,10.123,,upsert\n",
        "FP-1,plan.pdf,Plan,Road,,,,53.456,upsert\n",
    ],
)
def test_pdf_package_requires_paired_longitude_latitude(coordinate_row):
    with pytest.raises(ImportValidationError, match="supplied together"):
        parse_pdf_package(
            payload=package(fire_manifest(coordinate_row), **{"plan.pdf": b"%PDF-1.4\n"}),
            domain="fire_plans",
        )


@pytest.mark.parametrize("member", ["../plan.pdf", "/plan.pdf", "extra.pdf"])
def test_pdf_package_rejects_undeclared_or_unsafe_members(member):
    manifest = fire_manifest("FP-1,plan.pdf,School,Main 1,,,,,upsert\n")
    files = {"plan.pdf": b"%PDF", member: b"%PDF"}
    with pytest.raises(ImportValidationError):
        parse_pdf_package(payload=package(manifest, **files), domain="fire_plans")
