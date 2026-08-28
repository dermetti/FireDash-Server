"""Stage D PostgreSQL lifecycle regressions for reusable Fire Plan PDFs."""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from django.db import transaction
from django.test import override_settings

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications import document_artifacts
from apps.publications.artifacts import cleanup_stale_artifacts
from apps.publications.document_artifacts import (
    cleanup_unreferenced_document_artifacts,
    release_terminal_document_artifact_references,
)
from apps.publications.fire_plan_v2_delivery import ensure_generation_key
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    FirePlanDocumentArtifact,
    FirePlanDocumentArtifactCleanup,
    FirePlanGenerationKey,
    FirePlanGenerationManifest,
    PublicationFirePlanArtifactReference,
)
from apps.publications.paths import (
    document_artifact_relative_path,
    publication_artifact_relative_path,
)
from apps.publications.retention import run_publication_retention
from apps.publications.services import rollback_publication
from apps.reference_data.models import FirePlan


@pytest.fixture
def v2_retention_scope(db, tmp_path):
    admin = User.objects.create_user("v2-retention@example.test", "V2 Retention", "password")
    department = Department.objects.create(name="V2 Retention", short_code="V2R", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp",
    ):
        yield admin, department, scope, tmp_path / "artifacts"


def _publication(*, department, scope, version, status, usable=False):
    publication_id = uuid.uuid4()
    artifact_path = (
        publication_artifact_relative_path(
            department_id=department.id, publication_id=publication_id
        )
        if usable
        else ""
    )
    publication = DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        dataset_type_code="department_fire_plans",
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=version,
        source_snapshot={"fire_plans": []},
        status=status,
        artifact_ready=usable,
        artifact_status=(
            DatasetPublication.ArtifactStatus.READY
            if usable
            else DatasetPublication.ArtifactStatus.PENDING
        ),
        artifact_path=artifact_path,
        artifact_size=1 if usable else None,
        artifact_sha256="a" * 64 if usable else "",
        artifact_nonce=b"n" * 12 if usable else None,
        artifact_wrapped_cek=b"w" * 40 if usable else None,
        artifact_encryption_algorithm="AES-256-GCM" if usable else "",
        artifact_wrapping_algorithm="AES-KW-RFC3394" if usable else "",
        artifact_kek_version="test" if usable else "",
        artifact_signature=b"s" * 64 if usable else None,
        artifact_signature_algorithm="Ed25519" if usable else "",
        artifact_signing_key_version="test" if usable else "",
    )
    # The rollback contract remains v1-compatible during this dormant
    # transition; the v1 ZIP metadata is deliberately not replaced.
    return publication


def _artifact(*, department, root, identifier):
    plan = FirePlan.objects.create(
        department=department,
        external_identifier=identifier,
        object_name=identifier,
        address="Test Street",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename=f"{identifier}.pdf",
        file_size=1,
        page_count=1,
        sha256=hashlib.sha256(identifier.encode()).hexdigest(),
        uploaded_by=department.created_by,
    )
    artifact = FirePlanDocumentArtifact.objects.create(
        fire_plan=plan,
        sanitized_pdf_sha256=plan.sha256,
        artifact_path=document_artifact_relative_path(artifact_id=uuid.uuid4()),
        ciphertext_size=1,
        ciphertext_sha256="c" * 64,
        nonce=b"n" * 12,
        wrapped_cek=b"w" * 40,
        encryption_algorithm="AES-256-GCM",
        wrapping_algorithm="AES-KW-RFC3394",
        kek_version="test",
        signature=b"s" * 64,
        signature_algorithm="Ed25519",
        signing_key_version="test",
    )
    path = root / artifact.artifact_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    return plan, artifact


def _reference(*, publication, plan, artifact):
    return PublicationFirePlanArtifactReference.objects.create(
        publication=publication, fire_plan=plan, document_artifact=artifact
    )


def _delivery_state(*, publication):
    key = FirePlanGenerationKey.objects.create(
        publication=publication,
        wrapped_key=b"k" * 40,
        wrapping_algorithm="AES-KW-RFC3394",
        kek_version="test",
    )
    manifest = FirePlanGenerationManifest.objects.create(
        publication=publication,
        payload={"format": "fire-plan-generation-v2", "documents": []},
        signature=b"s" * 64,
        signature_algorithm="Ed25519",
        signing_key_version="test",
    )
    return key, manifest


@pytest.mark.django_db(transaction=True)
def test_shared_artifacts_survive_terminal_release_until_final_reference(v2_retention_scope):
    _, department, scope, root = v2_retention_scope
    old = _publication(
        department=department, scope=scope, version=40, status=DatasetPublication.Status.SUPERSEDED
    )
    middle = _publication(
        department=department, scope=scope, version=41, status=DatasetPublication.Status.SUPERSEDED
    )
    current = _publication(
        department=department,
        scope=scope,
        version=42,
        status=DatasetPublication.Status.PUBLISHED,
        usable=True,
    )
    plan_a, a1 = _artifact(department=department, root=root, identifier="A1")
    plan_b, b1 = _artifact(department=department, root=root, identifier="B1")
    plan_c, c1 = _artifact(department=department, root=root, identifier="C1")
    _reference(publication=old, plan=plan_a, artifact=a1)
    _reference(publication=old, plan=plan_b, artifact=b1)
    _reference(publication=old, plan=plan_c, artifact=c1)
    _reference(publication=middle, plan=plan_a, artifact=a1)
    _reference(publication=middle, plan=plan_b, artifact=b1)
    _reference(publication=middle, plan=plan_c, artifact=c1)
    _reference(publication=current, plan=plan_a, artifact=a1)
    _reference(publication=current, plan=plan_b, artifact=b1)
    _reference(publication=current, plan=plan_c, artifact=c1)

    with transaction.atomic():
        old.status = DatasetPublication.Status.OBSOLETE
        old.save(update_fields=("status",))
        assert release_terminal_document_artifact_references(publication=old) == 0
    assert FirePlanDocumentArtifact.objects.count() == 3
    assert all((root / artifact.artifact_path).exists() for artifact in (a1, b1, c1))

    with transaction.atomic():
        middle.status = DatasetPublication.Status.OBSOLETE
        middle.save(update_fields=("status",))
        assert release_terminal_document_artifact_references(publication=middle) == 0
    assert FirePlanDocumentArtifact.objects.count() == 3
    assert PublicationFirePlanArtifactReference.objects.filter(publication=current).count() == 3

    with transaction.atomic():
        current.status = DatasetPublication.Status.OBSOLETE
        current.save(update_fields=("status",))
        assert release_terminal_document_artifact_references(publication=current) == 3
        assert all((root / artifact.artifact_path).exists() for artifact in (a1, b1, c1))
    assert not FirePlanDocumentArtifact.objects.exists()
    assert not FirePlanDocumentArtifactCleanup.objects.exists()
    assert not any(root.rglob("artifact.bin"))


@pytest.mark.django_db(transaction=True)
def test_document_cleanup_is_post_commit_and_failure_is_retryable(v2_retention_scope, monkeypatch):
    _, department, scope, root = v2_retention_scope
    publication = _publication(
        department=department, scope=scope, version=1, status=DatasetPublication.Status.BUILDING
    )
    plan, artifact = _artifact(department=department, root=root, identifier="A")
    _reference(publication=publication, plan=plan, artifact=artifact)
    path = root / artifact.artifact_path

    with pytest.raises(RuntimeError), transaction.atomic():
        publication.status = DatasetPublication.Status.CANCELLED
        publication.save(update_fields=("status",))
        release_terminal_document_artifact_references(publication=publication)
        raise RuntimeError("rollback")
    assert path.exists()
    assert FirePlanDocumentArtifact.objects.filter(pk=artifact.id).exists()

    monkeypatch.setattr(
        document_artifacts,
        "remove_artifact_path",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    with transaction.atomic():
        publication.refresh_from_db()
        publication.status = DatasetPublication.Status.CANCELLED
        publication.save(update_fields=("status",))
        release_terminal_document_artifact_references(publication=publication)
        assert path.exists()
    assert path.exists()
    assert FirePlanDocumentArtifactCleanup.objects.filter(artifact_id=artifact.id).exists()

    monkeypatch.undo()
    assert cleanup_unreferenced_document_artifacts() == 1
    assert not path.exists()
    assert not FirePlanDocumentArtifactCleanup.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_rollback_preserves_retained_v2_document_membership(v2_retention_scope):
    admin, department, scope, root = v2_retention_scope
    previous = _publication(
        department=department,
        scope=scope,
        version=40,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    current = _publication(
        department=department,
        scope=scope,
        version=41,
        status=DatasetPublication.Status.PUBLISHED,
        usable=True,
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))
    plan, artifact = _artifact(department=department, root=root, identifier="Rollback")
    _reference(publication=previous, plan=plan, artifact=artifact)
    key, manifest = _delivery_state(publication=previous)

    restored = rollback_publication(actor=admin, publication=previous)

    restored.refresh_from_db()
    current.refresh_from_db()
    assert restored.status == DatasetPublication.Status.PUBLISHED
    assert current.status == DatasetPublication.Status.SUPERSEDED
    assert PublicationFirePlanArtifactReference.objects.filter(publication=restored).exists()
    assert (root / artifact.artifact_path).exists()
    assert FirePlanGenerationKey.objects.filter(pk=key.pk).exists()
    assert FirePlanGenerationManifest.objects.filter(pk=manifest.pk).exists()
    assert ensure_generation_key(publication=restored).pk == key.pk


@pytest.mark.django_db(transaction=True)
def test_retention_keeps_protected_v2_predecessor_and_its_delivery_state(v2_retention_scope):
    _, department, scope, root = v2_retention_scope
    obsolete_candidate = _publication(
        department=department,
        scope=scope,
        version=40,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    protected = _publication(
        department=department,
        scope=scope,
        version=41,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    newer_predecessor = _publication(
        department=department,
        scope=scope,
        version=42,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    current = _publication(
        department=department,
        scope=scope,
        version=43,
        status=DatasetPublication.Status.PUBLISHED,
        usable=True,
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))
    plan, artifact = _artifact(department=department, root=root, identifier="Protected")
    _reference(publication=obsolete_candidate, plan=plan, artifact=artifact)
    _reference(publication=protected, plan=plan, artifact=artifact)
    key, manifest = _delivery_state(publication=protected)

    result = run_publication_retention()

    obsolete_candidate.refresh_from_db()
    protected.refresh_from_db()
    newer_predecessor.refresh_from_db()
    assert result["obsoleted"] == 1
    assert obsolete_candidate.status == DatasetPublication.Status.OBSOLETE
    assert protected.status == newer_predecessor.status == DatasetPublication.Status.SUPERSEDED
    assert PublicationFirePlanArtifactReference.objects.filter(publication=protected).exists()
    assert (root / artifact.artifact_path).exists()
    assert FirePlanGenerationKey.objects.filter(pk=key.pk).exists()
    assert FirePlanGenerationManifest.objects.filter(pk=manifest.pk).exists()
    assert ensure_generation_key(publication=protected).pk == key.pk


@pytest.mark.django_db(transaction=True)
def test_stale_uncommitted_document_promotion_is_eventually_cleaned(v2_retention_scope):
    _, _, _, root = v2_retention_scope
    artifact_id = uuid.uuid4()
    path = root / document_artifact_relative_path(artifact_id=artifact_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"uncommitted ciphertext")
    os.utime(path, (1, 1))

    with override_settings(PUBLICATION_ARTIFACT_STALE_SECONDS=1):
        assert cleanup_stale_artifacts() == 1
    assert not path.exists()
