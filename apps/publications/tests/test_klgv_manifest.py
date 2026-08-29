# ruff: noqa: E501
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.gis.geos import Point
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.document_v2 import build_document_v2_generation, build_document_v2_manifest
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    DocumentArtifact,
    PublicationDocumentArtifactReference,
)
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import KlgvPlan


@pytest.mark.django_db
def test_klgv_snapshot_uses_canonical_metadata(settings, tmp_path):
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

    snapshot = build_source_payload(
        definition=get_dataset_definition("department_klgv_plans"),
        department=department,
        station=None,
    )
    item = snapshot["klgv_plans"][0]
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


@pytest.mark.django_db(transaction=True)
def test_klgv_v2_generation_is_complete_and_reuses_its_immutable_pdf(settings, tmp_path):
    settings.REFERENCE_DATA_ACCEPTED_ROOT = tmp_path / "accepted"
    actor = User.objects.create_user("klgv-v2@example.test", "KLGV", "safe-password")
    department = Department.objects.create(name="KLGV v2", short_code="K2", created_by=actor)
    pdf = b"%PDF-1.4\nKLGV\n%%EOF\n"
    digest = hashlib.sha256(pdf).hexdigest()
    plan = KlgvPlan.objects.create(
        department=department, external_identifier="K-2", object_name="KLGV",
        address="Garden Way 2", postal_code="22041", city="Hamburg", path="plans/k2.pdf",
        original_filename="k2.pdf", file_size=len(pdf), page_count=1,
        source_pdf_sha256=digest, sha256=digest, uploaded_by=actor,
    )
    accepted = settings.REFERENCE_DATA_ACCEPTED_ROOT / plan.path
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(pdf)
    scope = DatasetScopeState.objects.create(
        department=department, station=None, dataset_type_code="department_klgv_plans"
    )
    snapshot = build_source_payload(
        definition=get_dataset_definition("department_klgv_plans"), department=department, station=None
    )
    (tmp_path / "kek").write_bytes(base64.b64encode(b"a" * 32))
    (tmp_path / "signing").write_bytes(b"b" * 32)
    (tmp_path / "ring.json").write_text(
        '{"keys":{"1":"'
        + base64.b64encode(
            Ed25519PrivateKey.from_private_bytes(b"b" * 32).public_key().public_bytes_raw()
        ).decode("ascii")
        + '"}}',
        encoding="ascii",
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "tmp",
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=tmp_path / "ring.json",
    ):
        first = DatasetPublication.objects.create(
            department=department, scope_state=scope, dataset_type_code="department_klgv_plans",
            version_number=1, schema_version=2, source_revision=1, source_snapshot=snapshot,
            status=DatasetPublication.Status.BUILDING,
        )
        build_document_v2_generation(publication=first)
        manifest = build_document_v2_manifest(publication=first)
        second = DatasetPublication.objects.create(
            department=department, scope_state=scope, dataset_type_code="department_klgv_plans",
            version_number=2, schema_version=2, source_revision=2, source_snapshot=snapshot,
            status=DatasetPublication.Status.BUILDING,
        )
        build_document_v2_generation(publication=second)
    assert not first.artifact_path
    assert PublicationDocumentArtifactReference.objects.filter(publication=first).count() == 1
    assert DocumentArtifact.objects.count() == 1
    assert manifest.payload["dataset_type"] == "department_klgv_plans"
    assert manifest.payload["documents"][0]["klgv_plan"]["id"] == str(plan.id)
