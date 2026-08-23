import io
import zipfile

import pytest

from apps.ingestion.parsers import ImportValidationError
from apps.ingestion.pdf_packages import FIRE_PLAN_MANIFEST_NAME, parse_pdf_package


def package(manifest: str, **files: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(FIRE_PLAN_MANIFEST_NAME, manifest)
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def raw_package(members: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


def fire_manifest(rows: str) -> str:
    return (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,fsd_location,bmz_location,rwa_info,action\n"
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


def test_fire_plan_rejects_legacy_manifest_csv_alias():
    manifest = fire_manifest("FP-1,plan.pdf,School,Main 1,,,,,upsert\n")
    payload = raw_package({"manifest.csv": manifest, "plan.pdf": b"%PDF-1.4\n"})
    with pytest.raises(ImportValidationError, match="fire-plans-manifest-v1.csv"):
        parse_pdf_package(payload=payload, domain="fire_plans")


def test_fire_plan_missing_versioned_manifest_is_rejected():
    payload = raw_package({"plan.pdf": b"%PDF-1.4\n"})
    with pytest.raises(ImportValidationError, match="fire-plans-manifest-v1.csv"):
        parse_pdf_package(payload=payload, domain="fire_plans")


def test_fire_plan_duplicate_versioned_manifest_is_rejected():
    manifest = fire_manifest("FP-1,plan.pdf,School,Main 1,,,,,upsert\n")
    payload = raw_package(
        {
            FIRE_PLAN_MANIFEST_NAME: manifest,
            "dupe.csv": manifest,
            "plan.pdf": b"%PDF-1.4\n",
        }
    )
    # The second versioned manifest is an undeclared member.
    with pytest.raises(ImportValidationError):
        parse_pdf_package(payload=payload, domain="fire_plans")


def test_fire_plan_unicode_filename_is_accepted():
    manifest = fire_manifest("FP-1,l\u00f6schplan.pdf,School,Main 1,,,,,upsert\n")
    entries = parse_pdf_package(
        payload=package(manifest, **{"l\u00f6schplan.pdf": b"%PDF-1.4\n"}),
        domain="fire_plans",
    )
    assert entries[0].filename == "l\u00f6schplan.pdf"
    assert entries[0].pdf_bytes == b"%PDF-1.4\n"
