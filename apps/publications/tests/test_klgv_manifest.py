import hashlib
import io
import json
import zipfile

import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_artifact
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import KlgvPlan


@pytest.mark.django_db
def test_klgv_artifact_uses_canonical_metadata_and_deterministic_path(settings, tmp_path):
    settings.REFERENCE_DATA_ACCEPTED_ROOT = tmp_path
    actor = User.objects.create_user("klgv-manifest@example.test", "KLGV", "safe-password")
    department = Department.objects.create(name="KLGV", short_code="KLG", created_by=actor)
    document = b"%PDF-1.4\n%%EOF\n"
    digest = hashlib.sha256(document).hexdigest()
    plan = KlgvPlan.objects.create(
        department=department,
        external_identifier="K-1",
        object_name="Garden plan",
        address="Garden Way 1",
        postal_code="22041",
        city="Hamburg",
        location=Point(10.000992, 53.551323, srid=4326),
        path="plans/11111111-1111-1111-1111-111111111111.pdf",
        original_filename="uploaded.pdf",
        file_size=len(document),
        page_count=1,
        source_pdf_sha256=digest,
        sha256=digest,
        uploaded_by=actor,
    )
    accepted = tmp_path / plan.path
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(document)

    artifact = build_artifact(
        definition=get_dataset_definition("department_klgv_plans"),
        department=department,
        station=None,
        source_revision=7,
    )
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        item = manifest["klgv_plans"][0]
        assert item == {
            "id": str(plan.id),
            "external_identifier": "K-1",
            "object_name": "Garden plan",
            "address": "Garden Way 1",
            "postal_code": "22041",
            "city": "Hamburg",
            "longitude": 10.000992,
            "latitude": 53.551323,
            "sha256": digest,
            "page_count": 1,
            "path": f"plans/{plan.id}.pdf",
        }
        assert archive.read(item["path"]) == document
