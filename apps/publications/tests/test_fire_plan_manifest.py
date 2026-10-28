"""Regression tests for the distributed Fire Plan publication manifest schema."""

import hashlib
import json
import uuid

import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.document_dataset_adapters import document_dataset_adapter
from apps.publications.models import DatasetPublication
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
    "fsd_location": "FSD left of entrance",
    "bmz_location": "First floor",
    "rwa_info": "Manual trigger",
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


def _build_manifest(department, accepted_root, source_revision=42):
    definition = get_dataset_definition("department_fire_plans")
    snapshot = build_source_payload(definition=definition, department=department, station=None)
    publication = DatasetPublication(
        department=department,
        dataset_type_code=definition.code,
        schema_version=2,
        source_snapshot=snapshot,
    )
    adapter = document_dataset_adapter(dataset_type_code=definition.code)
    return {"fire_plans": [entry.metadata for entry in adapter.frozen_documents(publication)]}


def _build_artifact_bytes(department, accepted_root, source_revision=42):
    return json.dumps(_build_manifest(department, accepted_root), sort_keys=True).encode()


@pytest.mark.django_db
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
        "fsd_location",
        "bmz_location",
        "rwa_info",
        "longitude",
        "latitude",
        "sha256",
        "page_count",
    }
    assert entry == {
        "id": str(plan.id),
        "external_identifier": "SITE-A",
        "object_name": "Das Rauhe Haus",
        "address": "Am Stadtrand 56 und 56 a",
        "postal_code": "22047",
        "city": "Hamburg",
        "fsd_location": "FSD left of entrance",
        "bmz_location": "First floor",
        "rwa_info": "Manual trigger",
        "longitude": 10.09873774,
        "latitude": 53.59229519,
        "sha256": PDF_SHA256,
        "page_count": 12,
    }


@pytest.mark.django_db
def test_fire_plan_manifest_does_not_swap_coordinates(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(admin, department, accepted_root, location=Point(9.9988, 48.7776, srid=4326))

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["longitude"] == 9.9988
    assert entry["latitude"] == 48.7776
    assert entry["longitude"] != entry["latitude"]


@pytest.mark.django_db
def test_fire_plan_missing_fields_publish_null_per_document(manifest_context):
    admin, department, accepted_root = manifest_context
    plan_0 = _create_plan(
        admin,
        department,
        accepted_root,
        external_identifier="",
        address="Wandsbeker Zollstraße 95",
    )
    plan_1 = _create_plan(
        admin, department, accepted_root, external_identifier="SITE-1", location=None
    )
    plan_2 = _create_plan(
        admin,
        department,
        accepted_root,
        external_identifier="SITE-2",
        fsd_location="",
        bmz_location="",
        rwa_info="",
    )

    manifest = _build_manifest(department, accepted_root)
    entries = {entry["id"]: entry for entry in manifest["fire_plans"]}
    assert set(entries) == {str(plan_0.id), str(plan_1.id), str(plan_2.id)}
    entry = entries[str(plan_0.id)]
    assert entry["external_identifier"] is None
    assert entry["address"] == "Wandsbeker Zollstraße 95"
    assert entry["longitude"] == 10.09873774
    assert entry["latitude"] == 53.59229519

    entry = entries[str(plan_1.id)]
    assert entry["external_identifier"] == "SITE-1"
    assert entry["longitude"] is None
    assert entry["latitude"] is None

    entry = entries[str(plan_2.id)]
    assert entry["external_identifier"] == "SITE-2"
    assert entry["fsd_location"] is None
    assert entry["bmz_location"] is None
    assert entry["rwa_info"] is None


@pytest.mark.django_db
def test_fire_plan_manifest_preserves_existing_pdf_metadata(manifest_context):
    admin, department, accepted_root = manifest_context
    plan = _create_plan(admin, department, accepted_root)

    entry = _build_manifest(department, accepted_root)["fire_plans"][0]

    assert entry["id"] == str(plan.id)
    assert entry["sha256"] == PDF_SHA256
    assert entry["page_count"] == 12
    assert "path" not in entry


@pytest.mark.django_db
def test_fire_plan_manifest_is_deterministic(manifest_context):
    admin, department, accepted_root = manifest_context
    _create_plan(admin, department, accepted_root, **FULL_PLAN_FIELDS)

    first = _build_artifact_bytes(department, accepted_root)
    second = _build_artifact_bytes(department, accepted_root)

    assert first == second
