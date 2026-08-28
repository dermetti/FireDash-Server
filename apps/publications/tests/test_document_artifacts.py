import hashlib
import os
import uuid

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications import artifacts
from apps.publications.artifacts import _document_artifact_signature_payload
from apps.publications.document_artifacts import get_or_create_fire_plan_document_artifact
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    FirePlanDocumentArtifact,
    PublicationFirePlanArtifactReference,
)
from apps.reference_data.models import FirePlan


@pytest.fixture
def document_context(db):
    admin = User.objects.create_user(
        "document-artifact@example.test", "Document Admin", "safe-password"
    )
    department = Department.objects.create(name="Document Dept", short_code="DOC", created_by=admin)
    plan = FirePlan.objects.create(
        department=department,
        external_identifier="SITE-A",
        object_name="Original title",
        address="Example street 1",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="site-a.pdf",
        file_size=1,
        page_count=1,
        sha256="0" * 64,
        uploaded_by=admin,
    )
    return admin, department, plan


@pytest.fixture
def document_artifact_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="test-kek-v1",
        PUBLICATION_SIGNING_KEY_VERSION="test-signing-v1",
    ):
        yield tmp_path


@pytest.mark.django_db(transaction=True)
def test_document_artifact_reuses_only_same_plan_and_sanitized_content(
    document_context, document_artifact_settings
):
    _, department, plan = document_context
    first, created = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"sanitized PDF"
    )
    assert created
    reused, created = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"sanitized PDF"
    )
    assert not created
    assert reused.id == first.id

    # Metadata belongs to a future generation, not the distributed PDF bytes.
    plan.object_name = "Renamed plan"
    plan.save(update_fields=("object_name",))
    metadata_only, created = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"sanitized PDF"
    )
    assert not created
    assert metadata_only.id == first.id

    changed, created = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"changed sanitized PDF"
    )
    assert created
    assert changed.id != first.id
    assert changed.nonce != first.nonce
    assert changed.wrapped_cek != first.wrapped_cek

    other_plan = FirePlan.objects.create(
        department=department,
        external_identifier="SITE-B",
        object_name="Other plan",
        address="Example street 2",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="site-b.pdf",
        file_size=1,
        page_count=1,
        sha256="1" * 64,
        uploaded_by=plan.uploaded_by,
    )
    isolated, created = get_or_create_fire_plan_document_artifact(
        fire_plan=other_plan, sanitized_pdf=b"sanitized PDF"
    )
    assert created
    assert isolated.id != first.id
    assert (
        FirePlanDocumentArtifact.objects.filter(
            fire_plan=plan, sanitized_pdf_sha256=hashlib.sha256(b"sanitized PDF").hexdigest()
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_document_artifact_crypto_is_signed_decryptable_and_generation_independent(
    document_context, document_artifact_settings
):
    _, _, plan = document_context
    artifact, _ = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"safe PDF"
    )
    ciphertext = (document_artifact_settings / "artifacts" / artifact.artifact_path).read_bytes()
    assert artifact.ciphertext_size == len(ciphertext)
    assert artifact.ciphertext_sha256 == hashlib.sha256(ciphertext).hexdigest()
    assert artifact.sanitized_pdf_sha256 == hashlib.sha256(b"safe PDF").hexdigest()
    cek = keywrap.aes_key_unwrap(b"k" * 32, bytes(artifact.wrapped_cek))
    assert AESGCM(cek).decrypt(bytes(artifact.nonce), ciphertext, None) == b"safe PDF"
    payload = _document_artifact_signature_payload(
        artifact_id=artifact.id,
        fire_plan_id=plan.id,
        sanitized_pdf_sha256=artifact.sanitized_pdf_sha256,
        wrapped_cek=bytes(artifact.wrapped_cek),
        nonce=bytes(artifact.nonce),
        ciphertext=ciphertext,
    )
    assert b"version_number" not in payload and b"schema_version" not in payload
    Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
        bytes(artifact.signature), payload
    )


@pytest.mark.django_db(transaction=True)
def test_document_artifact_database_constraints_and_historical_reference(
    document_context, document_artifact_settings
):
    admin, department, plan = document_context
    artifact, _ = get_or_create_fire_plan_document_artifact(
        fire_plan=plan, sanitized_pdf=b"safe PDF"
    )
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            FirePlanDocumentArtifact.objects.create(
                fire_plan=plan,
                sanitized_pdf_sha256=artifact.sanitized_pdf_sha256,
                artifact_path="documents/duplicate/artifact.bin",
                ciphertext_size=1,
                ciphertext_sha256="a" * 64,
                nonce=b"0" * 12,
                wrapped_cek=b"wrapped",
                encryption_algorithm="AES-256-GCM",
                wrapping_algorithm="AES-KW-RFC3394",
                kek_version="1",
                signature=b"signature",
                signature_algorithm="Ed25519",
                signing_key_version="1",
            )

    scope = DatasetScopeState.objects.create(
        department=department, station=None, dataset_type_code="department_fire_plans"
    )
    publication = DatasetPublication.objects.create(
        department=department,
        station=None,
        dataset_type_code="department_fire_plans",
        scope_state=scope,
        version_number=1,
        schema_version=1,
        source_revision=1,
        created_by=admin,
    )
    reference = PublicationFirePlanArtifactReference.objects.create(
        publication=publication, fire_plan=plan, document_artifact=artifact
    )
    assert reference.document_artifact_id == artifact.id
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            PublicationFirePlanArtifactReference.objects.create(
                publication=publication, fire_plan=plan, document_artifact=artifact
            )

    other_department = Department.objects.create(
        name="Other Document Dept", short_code="ODC", created_by=admin
    )
    other_plan = FirePlan.objects.create(
        department=other_department,
        external_identifier="OTHER-SITE",
        object_name="Other tenant plan",
        address="Other street 1",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="other.pdf",
        file_size=1,
        page_count=1,
        sha256="2" * 64,
        uploaded_by=admin,
    )
    with transaction.atomic():
        with pytest.raises(DatabaseError, match="Document artifact must belong"):
            PublicationFirePlanArtifactReference.objects.create(
                publication=publication, fire_plan=other_plan, document_artifact=artifact
            )


def test_document_artifact_promotion_is_atomic_and_identity_addressed(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    root = tmp_path / "artifacts"
    artifact_id = uuid.uuid4()
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(
        artifacts.os,
        "replace",
        lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst))[1],
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=root,
        PUBLICATION_ARTIFACT_TEMP_ROOT=root / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
    ):
        metadata = artifacts.build_encrypted_document_artifact(
            artifact_id=artifact_id, fire_plan_id=uuid.uuid4(), sanitized_pdf=b"safe PDF"
        )
    assert metadata["artifact_path"] == f"documents/{artifact_id}/artifact.bin"
    assert replaced == [
        (
            root / ".tmp" / str(artifact_id) / "artifact.bin",
            root / "documents" / str(artifact_id) / "artifact.bin",
        )
    ]
