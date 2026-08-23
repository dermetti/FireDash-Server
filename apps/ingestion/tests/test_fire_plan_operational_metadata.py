import io
import zipfile

import pytest

from apps.ingestion.pdf_packages import FIRE_PLAN_MANIFEST_NAME, parse_pdf_package
from apps.ingestion.parsers import ImportValidationError


def _package(manifest: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(FIRE_PLAN_MANIFEST_NAME, manifest)
        archive.writestr("plan.pdf", b"%PDF-1.4\n")
    return output.getvalue()


def test_fire_plan_package_parses_all_operational_location_fields():
    manifest = (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,"
        "fsd_location,bmz_location,rwa_info,action\n"
        "FP-1,plan.pdf,Das Rauhe Haus,Am Stadtrand 56,22047,Hamburg,10.09873774,"
        "53.59229519,FSD links,Erstes Obergeschoss,Handtaster,upsert\n"
    )

    entry = parse_pdf_package(payload=_package(manifest), domain="fire_plans")[0]

    assert entry.fsd_location == "FSD links"
    assert entry.bmz_location == "Erstes Obergeschoss"
    assert entry.rwa_info == "Handtaster"
    assert entry.longitude == 10.09873774
    assert entry.latitude == 53.59229519


def test_fire_plan_package_rejects_stale_manifest_header():
    manifest = (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
        "FP-1,plan.pdf,Plan,Road,1,Town,10,53,upsert\n"
    )

    with pytest.raises(ImportValidationError, match="columns"):
        parse_pdf_package(payload=_package(manifest), domain="fire_plans")
