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
    # This synthetic current attempt predates canonical source fixtures. Make
    # its source intentionally differ so the staged-attempt lifecycle under
    # test is reachable through the current fingerprint semantics.
    current.source_fingerprint = "a" * 64
    current.source_snapshot = {"synthetic": "current"}
    current.save(update_fields=("source_fingerprint", "source_snapshot"))
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


def _running_attempt(*, admin, department, scope):
    request_rebuild(actor=admin, department=department, dataset_type_code=scope.dataset_type_code)
    job = claim_next_job()
    assert job is not None and job.build_publication_id is not None
    return job


def _cancel_while_holding_scope(*, admin_id, publication_id, scope_id, barrier):
    close_old_connections()
    try:
        admin = User.objects.get(pk=admin_id)
        publication = DatasetPublication.objects.get(pk=publication_id)
        with transaction.atomic():
            DatasetScopeState.objects.select_for_update().get(pk=scope_id)
            barrier.wait(timeout=10)
            cancel_publication_build(actor=admin, publication=publication)
        return "cancelled"
    finally:
        close_old_connections()


def _publish_while_holding_scope(*, job_id, department_id, publication_id, scope_id, barrier):
    close_old_connections()
    try:
        with transaction.atomic():
            DatasetScopeState.objects.select_for_update().get(pk=scope_id)
            barrier.wait(timeout=10)
            finalized = finalize_publication_job(
                job_id=job_id,
                summary={"active_count": 0, "source_revision": 1, "status_counts": {}},
                artifact=_metadata(Department.objects.get(pk=department_id), publication_id),
            )
        return finalized.status
    finally:
        close_old_connections()


def _publish_after_scope_lock(*, job_id, department_id, publication_id, barrier):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        finalized = finalize_publication_job(
            job_id=job_id,
            summary={"active_count": 0, "source_revision": 1, "status_counts": {}},
            artifact=_metadata(Department.objects.get(pk=department_id), publication_id),
        )
        return finalized.status
    finally:
        close_old_connections()


def _cancel_after_scope_lock(*, admin_id, publication_id, barrier):
    close_old_connections()
    try:
        admin = User.objects.get(pk=admin_id)
        publication = DatasetPublication.objects.get(pk=publication_id)
        barrier.wait(timeout=10)
        try:
            cancel_publication_build(actor=admin, publication=publication)
        except PublicationError:
            return "rejected"
        return "cancelled"
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
def test_concurrent_cancel_committing_first_prevents_publication_activation(lifecycle_scope):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-locking semantics are required for this regression test.")
    admin, _, department, _, scope = lifecycle_scope
    job = _running_attempt(admin=admin, department=department, scope=scope)
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelled = executor.submit(
            _cancel_while_holding_scope,
            admin_id=admin.id,
            publication_id=job.build_publication_id,
            scope_id=scope.id,
            barrier=barrier,
        )
        finalized = executor.submit(
            _publish_after_scope_lock,
            job_id=job.id,
            department_id=department.id,
            publication_id=job.build_publication_id,
            barrier=barrier,
        )
        assert cancelled.result(timeout=15) == "cancelled"
        assert finalized.result(timeout=15) == PublicationJob.Status.CANCELLED

    job.refresh_from_db()
    job.build_publication.refresh_from_db()
    scope.refresh_from_db()
    assert job.status == PublicationJob.Status.CANCELLED
    assert job.build_publication.status == DatasetPublication.Status.CANCELLED
    assert scope.current_published_publication is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_activation_committing_first_rejects_cancellation(lifecycle_scope):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-locking semantics are required for this regression test.")
    admin, _, department, _, scope = lifecycle_scope
    job = _running_attempt(admin=admin, department=department, scope=scope)
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        finalized = executor.submit(
            _publish_while_holding_scope,
            job_id=job.id,
            department_id=department.id,
            publication_id=job.build_publication_id,
            scope_id=scope.id,
            barrier=barrier,
        )
        cancelled = executor.submit(
            _cancel_after_scope_lock,
            admin_id=admin.id,
            publication_id=job.build_publication_id,
            barrier=barrier,
        )
        assert finalized.result(timeout=15) == PublicationJob.Status.SUCCEEDED
        assert cancelled.result(timeout=15) == "rejected"

    job.refresh_from_db()
    job.build_publication.refresh_from_db()
    scope.refresh_from_db()
    assert job.status == PublicationJob.Status.SUCCEEDED
    assert job.build_publication.status == DatasetPublication.Status.PUBLISHED
    assert scope.current_published_publication_id == job.build_publication_id
