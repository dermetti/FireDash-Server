import csv
import io
import zipfile
from pathlib import Path

from apps.ingestion.parsers import parse_hydrants, parse_personnel
from apps.ingestion.pdf_packages import manifest_member_name, parse_pdf_package

ROOT = Path(__file__).resolve().parents[1] / "static" / "ingestion" / "templates"
DOCS = Path(__file__).resolve().parents[3] / "docs" / "formats" / "fire-plans-v1.md"


def test_structured_templates_are_utf8_and_parse_with_the_real_importers():
    hydrant_csv = (ROOT / "hydrants-v1.csv").read_bytes()
    hydrant_geojson = (ROOT / "hydrants-v1.geojson").read_bytes()
    personnel_csv = (ROOT / "personnel-v1.csv").read_bytes()
    personnel_json = (ROOT / "personnel-v1.json").read_bytes()
    assert parse_hydrants(payload=hydrant_csv, import_format="csv")
    assert parse_hydrants(payload=hydrant_geojson, import_format="geojson")
    assert parse_personnel(payload=personnel_csv, import_format="csv")
    assert parse_personnel(payload=personnel_json, import_format="json")


def test_pdf_manifest_templates_have_the_exact_documented_columns():
    fire = (ROOT / "fire-plans-manifest-v1.csv").read_text(encoding="utf-8")
    klgv = (ROOT / "klgv-plans-manifest-v1.csv").read_text(encoding="utf-8")
    assert fire.splitlines()[0] == (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,"
        "fsd_location,bmz_location,rwa_info,action"
    )
    assert klgv.splitlines()[0] == "external_id,filename,title,category,action"
    assert not any(".xlsx" in value or ".xls" in value for value in (fire, klgv))
    assert fire.splitlines()[0] in DOCS.read_text(encoding="utf-8")


def test_pdf_manifest_templates_parse_with_the_real_package_importer():
    for name, domain in (
        ("fire-plans-manifest-v1.csv", "fire_plans"),
        ("klgv-plans-manifest-v1.csv", "klgv_plans"),
    ):
        manifest = (ROOT / name).read_bytes()
        rows = list(csv.DictReader(io.StringIO(manifest.decode("utf-8"))))
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(manifest_member_name(domain), manifest)
            for row in rows:
                archive.writestr(row["filename"], b"test-pdf")
        assert parse_pdf_package(payload=payload.getvalue(), domain=domain)
