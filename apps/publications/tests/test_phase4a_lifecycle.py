"""Phase 4A publication-attempt lifecycle regressions."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications import services
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import (
    PublicationError,
    cancel_publication_build,
    claim_next_job,
    delete_publication,
    delete_staged_publication,
    enqueue_publication_job,
    finalize_publication_job,
    request_rebuild,
    rollback_publication,
)


@pytest.fixture
def lifecycle_scope(db):
    admin = User.objects.create_user("phase4-admin@example.test", "Admin", "password")
    outsider = User.objects.create_user("phase4-outsider@example.test", "Outsider", "password")
    department = Department.objects.create(name="Phase 4", short_code="P4", created_by=admin)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=outsider)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants", source_revision=1
    )
    return admin, outsider, department, other, scope


def _metadata(department, publication_id):
    return {
        "artifact_path": f"{department.id}/{publication_id}/artifact.bin",
        "artifact_size": 1,
        "artifact_sha256": "a" * 64,
        "artifact_nonce": b"n" * 12,
        "artifact_wrapped_cek": b"w" * 40,
        "artifact_encryption_algorithm": "AES-256-GCM",
        "artifact_wrapping_algorithm": "AES-KW-RFC3394",
        "artifact_kek_version": "test",
        "artifact_signature": b"s" * 64,
        "artifact_signature_algorithm": "Ed25519",
        "artifact_signing_key_version": "test",
    }


def _successful(*, department, scope, version, status):
    publication_id = uuid.uuid4()
    publication = DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=scope.source_revision,
        status=status,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        **_metadata(department, publication_id),
    )
    return publication


@pytest.mark.django_db(transaction=True)
def test_staged_delete_preserves_attempt_identity_and_next_version(lifecycle_scope):
    admin, _, department, _, scope = lifecycle_scope
    job = enqueue_publication_job(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        requested_by=admin,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
    )
    assert job is not None and job.build_publication is not None
    staged = job.build_publication
    delete_staged_publication(actor=admin, publication=staged)
    staged.refresh_from_db()
    job.refresh_from_db()
    assert staged.status == DatasetPublication.Status.OBSOLETE
    assert job.status == PublicationJob.Status.OBSOLETE
    next_job = enqueue_publication_job(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        requested_by=admin,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
    )
    assert (
        next_job is not None and next_job.build_publication.version_number > staged.version_number
    )
    assert AuditEvent.objects.filter(
        action="publication.staged_deleted", target_uuid=staged.id
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_cancelled_build_cannot_finalize_or_activate(lifecycle_scope):
    admin, _, department, _, scope = lifecycle_scope
    request_rebuild(actor=admin, department=department, dataset_type_code=scope.dataset_type_code)
    job = claim_next_job()
    assert job is not None and job.build_publication is not None
    cancelled = cancel_publication_build(actor=admin, publication=job.build_publication)
    final = finalize_publication_job(
        job_id=job.id,
        summary={"active_count": 0, "source_revision": 1, "status_counts": {}},
        artifact=_metadata(department, cancelled.id),
    )
    cancelled.refresh_from_db()
    scope.refresh_from_db()
    assert final.status == PublicationJob.Status.CANCELLED
    assert cancelled.status == DatasetPublication.Status.CANCELLED
    assert scope.current_published_publication is None
    assert AuditEvent.objects.filter(
        action="publication.build_cancelled", target_uuid=cancelled.id
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_rollback_and_active_delete_require_scope_stability(lifecycle_scope):
    admin, _, department, _, scope = lifecycle_scope
    old = _successful(
        department=department, scope=scope, version=19, status=DatasetPublication.Status.SUPERSEDED
    )
    current = _successful(
        department=department, scope=scope, version=20, status=DatasetPublication.Status.PUBLISHED
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))
    staged_job = enqueue_publication_job(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        requested_by=admin,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
    )
    with pytest.raises(PublicationError, match="newer attempt"):
        rollback_publication(actor=admin, publication=old)
    with pytest.raises(PublicationError, match="newer attempt"):
        delete_publication(actor=admin, publication=current)
    delete_staged_publication(actor=admin, publication=staged_job.build_publication)
    restored = rollback_publication(actor=admin, publication=old)
    assert restored.status == DatasetPublication.Status.PUBLISHED
    current.refresh_from_db()
    assert current.status == DatasetPublication.Status.SUPERSEDED
    rollback_publication(actor=admin, publication=current)
    deleted = delete_publication(actor=admin, publication=current)
    deleted.refresh_from_db()
    old.refresh_from_db()
    scope.refresh_from_db()
    assert deleted.status == DatasetPublication.Status.OBSOLETE
    assert deleted.artifact_path
    assert deleted.artifact_status == DatasetPublication.ArtifactStatus.READY
    assert scope.current_published_publication_id == old.id


@pytest.mark.django_db(transaction=True)
def test_delete_active_without_safe_predecessor_and_cross_department_are_rejected(lifecycle_scope):
    admin, outsider, department, other, scope = lifecycle_scope
    current = _successful(
        department=department, scope=scope, version=1, status=DatasetPublication.Status.PUBLISHED
    )
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    with pytest.raises(PublicationError, match="no safe predecessor"):
        delete_publication(actor=admin, publication=current)
    with pytest.raises(PermissionDenied):
        delete_publication(actor=outsider, publication=current)


@pytest.mark.django_db(transaction=True)
def test_terminal_attempts_never_reuse_versions_after_cancel_or_failure(lifecycle_scope):
    admin, _, department, _, scope = lifecycle_scope
    request_rebuild(actor=admin, department=department, dataset_type_code=scope.dataset_type_code)
    job = claim_next_job()
    assert job is not None and job.build_publication is not None
    cancel_publication_build(actor=admin, publication=job.build_publication)
    next_job = enqueue_publication_job(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        requested_by=admin,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
    )
    assert next_job is not None
    assert next_job.build_publication.version_number > job.build_publication.version_number


@pytest.mark.django_db(transaction=True)
def test_artifact_cleanup_is_not_run_when_lifecycle_transaction_rolls_back(monkeypatch):
    removed = []
    monkeypatch.setattr(services, "remove_artifact_path", lambda path: removed.append(path))

    with pytest.raises(RuntimeError), transaction.atomic():
        services._schedule_artifact_removal("department/publication/artifact.bin")
        raise RuntimeError("rollback")
    assert removed == []

    with transaction.atomic():
        services._schedule_artifact_removal("department/publication/artifact.bin")
    assert removed == ["department/publication/artifact.bin"]


def _concurrent_rollback(*, admin_id, publication_id, barrier):
    close_old_connections()
    try:
        admin = User.objects.get(pk=admin_id)
        publication = DatasetPublication.objects.get(pk=publication_id)
        barrier.wait(timeout=10)
        try:
            rollback_publication(actor=admin, publication=publication)
        except PublicationError:
            return "rejected"
        return "rolled_back"
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
def test_concurrent_rollbacks_leave_exactly_one_active_publication(lifecycle_scope):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-locking semantics are required for this regression test.")
    admin, _, department, _, scope = lifecycle_scope
    old = _successful(
        department=department, scope=scope, version=19, status=DatasetPublication.Status.SUPERSEDED
    )
    current = _successful(
        department=department, scope=scope, version=20, status=DatasetPublication.Status.PUBLISHED
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(
                _concurrent_rollback,
                admin_id=admin.id,
                publication_id=old.id,
                barrier=barrier,
            )
            for _ in range(2)
        ]
        outcomes = [result.result(timeout=15) for result in results]

    assert sorted(outcomes) == ["rejected", "rolled_back"]
    assert (
        DatasetPublication.objects.filter(
            scope_state=scope, status=DatasetPublication.Status.PUBLISHED
        ).count()
        == 1
    )
