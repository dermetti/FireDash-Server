"""Review-wizard explanation helpers (non-DB)."""

from types import SimpleNamespace
from typing import cast

from django.contrib.gis.geos import Point

from apps.ingestion.services import (
    _document_changed_fields,
    _document_update_detail,
    _haversine_km,
    _identity_match,
    _mib_format,
)
from apps.reference_data.models import FirePlan


def test_identity_match_external_identifier():
    assert _identity_match(
        {"external_identifier": "FP-1", "address": "Road 1"}, domain="fire_plans"
    ) == {"strategy": "external_identifier", "value": "FP-1"}


def test_identity_match_address_fallback():
    assert _identity_match(
        {"external_identifier": "", "address": "Wandsbeker Zollstraße 95"}, domain="fire_plans"
    ) == {"strategy": "address_fallback", "value": "Wandsbeker Zollstraße 95"}


def test_haversine_km_is_plausible():
    berlin_hamburg = _haversine_km(52.52, 13.405, 53.5768409, 10.08110276)
    assert 250 < berlin_hamburg < 260
    assert _haversine_km(50.0, 10.0, 50.0, 10.0) == 0.0


def test_mib_format():
    assert _mib_format(1024 * 1024) == "1.00 MiB"


def test_document_changed_fields_includes_pdf_evidence_and_distance():
    current = SimpleNamespace(
        source_pdf_sha256="a" * 64,
        sanitized_pdf_sha256="b" * 64,
        sha256="b" * 64,
        file_size=1 * 1024 * 1024,
        page_count=4,
        object_name="Old",
        address="Road",
        postal_code="",
        city="",
        fsd_location="",
        bmz_location="",
        rwa_info="",
        location=Point(10.0, 50.0, srid=4326),
    )
    row = {
        "source_pdf_sha256": "c" * 64,
        "sanitized_pdf_sha256": "d" * 64,
        "file_size": 2 * 1024 * 1024,
        "page_count": 4,
        "title": "New",
        "address": "Road",
        "postal_code": "22041",
        "city": "Hamburg",
        "fsd_location": "FSD",
        "bmz_location": "BMZ",
        "rwa_info": "RWA",
        "longitude": 10.08110276,
        "latitude": 53.5768409,
    }

    fields = _document_changed_fields(current=current, row=row, model=FirePlan)

    by_name = {field["name"]: field for field in fields}
    assert by_name["source_pdf_sha256"]["current"] == "a" * 64
    assert by_name["source_pdf_sha256"]["proposed"] == "c" * 64
    assert by_name["pdf_size"]["current"] == "1.00 MiB"
    assert by_name["pdf_size"]["proposed"] == "2.00 MiB"
    assert by_name["postal_code"]["current"] == ""
    assert by_name["postal_code"]["proposed"] == "22041"
    assert "distance_km" in by_name["location"]
    assert cast(float, by_name["location"]["distance_km"]) > 300


def test_document_update_detail_includes_match_explanation():
    current = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        source_pdf_sha256="a" * 64,
        sanitized_pdf_sha256="b" * 64,
        sha256="b" * 64,
        file_size=1,
        page_count=1,
        object_name="Old",
        address="Road",
        postal_code="",
        city="",
        fsd_location="",
        bmz_location="",
        rwa_info="",
        location=Point(10.0, 50.0, srid=4326),
    )
    row = {
        "external_identifier": "",
        "address": "Road",
        "title": "New",
        "postal_code": "22041",
        "city": "Hamburg",
        "fsd_location": "",
        "bmz_location": "",
        "rwa_info": "",
        "longitude": 10.08110276,
        "latitude": 53.5768409,
        "source_pdf_sha256": "a" * 64,
        "sanitized_pdf_sha256": "b" * 64,
        "file_size": 1,
        "page_count": 1,
        "original_filename": "plan.pdf",
    }

    detail = _document_update_detail(current=current, row=row, model=FirePlan, domain="fire_plans")

    assert detail["matched_record_id"] == "11111111-1111-1111-1111-111111111111"
    assert detail["identity_strategy"] == "address_fallback"
    assert detail["matched_value"] == "Road"
    assert detail["incoming_filename"] == "plan.pdf"
