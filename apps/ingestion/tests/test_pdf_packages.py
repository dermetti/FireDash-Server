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
        "external_id,filename,object_name,street_address,postal_code,city,latitude,longitude,action\n"
        + rows
    )


def test_pdf_package_has_exact_member_identity_mapping():
    entries = parse_pdf_package(
        payload=package(
            fire_manifest("FP-1,plan.pdf,School,Main 1,12345,Town,50.1,8.2,upsert\n"),
            **{"plan.pdf": b"%PDF-1.4\n"},
        ),
        domain="fire_plans",
    )
    assert entries[0].external_identifier == "FP-1"
    assert entries[0].pdf_bytes == b"%PDF-1.4\n"


@pytest.mark.parametrize("member", ["../plan.pdf", "/plan.pdf", "extra.pdf"])
def test_pdf_package_rejects_undeclared_or_unsafe_members(member):
    manifest = fire_manifest("FP-1,plan.pdf,School,Main 1,,,,,upsert\n")
    files = {"plan.pdf": b"%PDF", member: b"%PDF"}
    with pytest.raises(ImportValidationError):
        parse_pdf_package(payload=package(manifest, **files), domain="fire_plans")
