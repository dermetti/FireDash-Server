import io
import zipfile

import pytest
from django.test import override_settings

from apps.ingestion.parsers import ImportValidationError, parse_hydrants
from apps.ingestion.pdf_packages import FIRE_PLAN_MANIFEST_NAME, parse_pdf_package


def geojson_payload(count: int) -> bytes:
    features = [
        (
            '{"type":"Feature","geometry":{"type":"Point","coordinates":[10.0,50.0]},'
            f'"properties":{{"external_identifier":"H-{i}","street":"",'
            '"house_number":"","location":"","hydrant_type":"",'
            '"diameter_mm":null,"status":"ACTIVE"}}'
        )
        for i in range(count)
    ]
    return ('{"type":"FeatureCollection","features":[' + ",".join(features) + "]}").encode()


@pytest.mark.slow
def test_geojson_10001_features_are_accepted():
    assert len(parse_hydrants(payload=geojson_payload(10_001), import_format="geojson")) == 10_001


@pytest.mark.slow
def test_geojson_38000_features_are_accepted():
    assert len(parse_hydrants(payload=geojson_payload(38_000), import_format="geojson")) == 38_000


@pytest.mark.slow
def test_geojson_50000_features_are_accepted():
    assert len(parse_hydrants(payload=geojson_payload(50_000), import_format="geojson")) == 50_000


def test_geojson_50001_features_are_rejected():
    with pytest.raises(ImportValidationError, match="feature limit exceeded"):
        parse_hydrants(payload=geojson_payload(50_001), import_format="geojson")


@override_settings(MAX_STRUCTURED_IMPORT_BYTES=100)
def test_geojson_byte_size_limit_is_enforced():
    with pytest.raises(ImportValidationError, match="size limit"):
        parse_hydrants(payload=geojson_payload(10), import_format="geojson")


def _package(manifest: str, files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(FIRE_PLAN_MANIFEST_NAME, manifest)
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


_FIRE_HEADER = (
    "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,fsd_location,bmz_location,rwa_info,action"
)


def _fire_manifest(count: int) -> str:
    rows = [f"FP-{i},p{i}.pdf,,,,,,,upsert" for i in range(count)]
    return "\n".join([_FIRE_HEADER, *rows]) + "\n"


def _fire_files(count: int) -> dict[str, bytes]:
    return {f"p{i}.pdf": b"%PDF-1.4\n" for i in range(count)}


@override_settings(MAX_INGEST_UPLOAD_BYTES=64)
def test_pdf_package_aggregate_size_limit_uses_ingest_upload_bytes():
    payload = _package(_fire_manifest(1), {"p0.pdf": b"x" * 512})
    with pytest.raises(ImportValidationError, match="size limit"):
        parse_pdf_package(payload=payload, domain="fire_plans")


@override_settings(MAX_PDF_INPUT_BYTES=100, MAX_INGEST_UPLOAD_BYTES=10_000)
def test_pdf_package_aggregate_zip_may_exceed_individual_pdf_limit():
    # Three 50-byte PDFs each obey the 100-byte individual PDF limit, while the
    # aggregate ZIP exceeds that individual ceiling: the aggregate is bounded by
    # MAX_INGEST_UPLOAD_BYTES, not MAX_PDF_INPUT_BYTES.
    payload = _package(_fire_manifest(3), {f"p{i}.pdf": b"x" * 50 for i in range(3)})
    assert len(payload) > 100
    assert len(parse_pdf_package(payload=payload, domain="fire_plans")) == 3


@override_settings(MAX_PDF_PACKAGE_EXPANDED_BYTES=100)
def test_pdf_package_rejects_excessive_uncompressed_size():
    payload = _package(_fire_manifest(1), {"p0.pdf": b"x" * 1000})
    with pytest.raises(ImportValidationError, match="expanded size"):
        parse_pdf_package(payload=payload, domain="fire_plans")


@override_settings(MAX_PDF_INPUT_BYTES=8)
def test_pdf_package_rejects_oversized_individual_pdf():
    payload = _package(_fire_manifest(1), {"p0.pdf": b"x" * 64})
    with pytest.raises(ImportValidationError, match="PDF exceeds"):
        parse_pdf_package(payload=payload, domain="fire_plans")


def test_pdf_package_accepts_250_documents():
    entries = parse_pdf_package(
        payload=_package(_fire_manifest(250), _fire_files(250)), domain="fire_plans"
    )
    assert len(entries) == 250


def test_pdf_package_rejects_251_documents():
    with pytest.raises(ImportValidationError):
        parse_pdf_package(
            payload=_package(_fire_manifest(251), _fire_files(251)), domain="fire_plans"
        )
