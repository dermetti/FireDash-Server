"""Regression tests for the distributed Fire Plan publication manifest schema."""

import hashlib
import io
import json
import uuid
import zipfile

import pytest
from django.contrib.gis.geos import Point
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_artifact
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan

PDF = b"%PDF-1.4\n1 0 obj\nendobj\n%%EOF\n"
PDF_SHA256 = hashlib.sha256(PDF).hexdigest()

FULL_PLAN_FIELDS = {
    "external_identifier": "SITE-A",
    "object_name": "Das Rauhe Haus",
    "address": "Am Stadtrand 56 und 56 a",
    "postal_code": "22047",
    "city": "Hamburg",
    "location": Point(10.09873774, 53.59229519, srid=4326),
}


@pytest.fixture
def manifest_context(db, tmp_path):
    admin = User.objects.create_user("manifest@example.test", "Manifest Admin", "safe-password")
    department = Department.objects.create(name="Manifest Dept", short_code="MNF", created_by=admin)
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir(parents=True)
    return admin, department, accepted_root


def _create_plan(admin, department, accepted_root, **overrides):
    fields = {
        "department": department,
        "external_identifier": "SITE-A",
        "object_name": "Site A",
        "address": "Am Stadtrand 56 und 56 a",
        "postal_code": "22047",
        "city": "Hamburg",
        "location": Point(10.09873774, 53.59229519, srid=4326),
        "document_key": f"{uuid.uuid4()}.pdf",
        "original_filename": "site-a.pdf",
        "file_size": len(PDF),
        "page_count": 12,
        "sha256": PDF_SHA256,
        "uploaded_by": admin,
    }
    fields.update(overrides)
    plan = FirePlan.objects.create(**fields)
    (accepted_root / plan.document_key).write_bytes(PDF)
    return plan


def _build_artifact_bytes(department, accepted_root, source_revision=42):
    definition = get_dataset_definition("department_fire_plans")
    with override_settings(REFERENCE_DATA_ACCEPTED_ROOT=accepted_root):
        return build_artifact(
            definition=definition,
            department=department,
            station=None,
            source_revision=source_revision,
        )


def _build_manifest(department, accepted_root, source_revision=42):
    artifact = _build_artifact_bytes(department, accepted_root, source_revision)
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        return json.loads(archive.read("manifest.json"))


@pytest.mark.django_db(transaction=True)
def test_fire_plan_manifest_publishes_exact_schema(manifest_context):
    admin, department, accepted_root = manifest_context
    plan = _create_plan(admin, department, accepted_root, **FULL_PLAN_FIELDS)

    manifest = _build_manifest(department, accepted_root)
    entry = manifest["fire_plans"][0]

    assert set(entry) == {
        "id",
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "sha256",
        "page_count",
        "path",
    }
    assert entry == {
        "id": str(plan.id),
        "external_identifier": "SITE-A",
        "object_name": "Das Rauhe Haus",
        "address": "Am Stadtrand 56 und 56 a",
        "postal_code": "22047",
        "city": "Hamburg",
        "longitude": 10.09873774,
        "latitude": 53.59229519,
        "sha256": PDF_SHA256,
        "page_count": 12,
        "path": f"plans/{plan.id}.pdf",
    }


@pytest.mark.django_db(transaction=True)
def test_fire_plan_manifest_does_not_swap_coordinates(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(admin, department, accepted_root, location=Point(9.9988, 48.7776, srid=4326))

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["longitude"] == 9.9988
    assert entry["latitude"] == 48.7776
    assert entry["longitude"] != entry["latitude"]


@pytest.mark.django_db(transaction=True)
def test_fire_plan_without_external_identifier_publishes_null(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(
        admin,
        department,
        accepted_root,
        external_identifier="",
        address="Wandsbeker Zollstraße 95",
    )

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["external_identifier"] is None
    assert entry["address"] == "Wandsbeker Zollstraße 95"


@pytest.mark.django_db(transaction=True)
def test_fire_plan_without_coordinates_publishes_null(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(admin, department, accepted_root, location=None)

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["longitude"] is None
    assert entry["latitude"] is None


@pytest.mark.django_db(transaction=True)
def test_fire_plan_manifest_preserves_existing_pdf_metadata(manifest_context):
    admin, department, accepted_root = manifest_context
    plan = _create_plan(admin, department, accepted_root)

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["id"] == str(plan.id)
    assert entry["sha256"] == PDF_SHA256
    assert entry["page_count"] == 12
    assert entry["path"] == f"plans/{plan.id}.pdf"


@pytest.mark.django_db(transaction=True)
def test_fire_plan_manifest_is_deterministic(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(admin, department, accepted_root, **FULL_PLAN_FIELDS)

    first = _build_artifact_bytes(department, accepted_root)
    second = _build_artifact_bytes(department, accepted_root)

    assert first == second
