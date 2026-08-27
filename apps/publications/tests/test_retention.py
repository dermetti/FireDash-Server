"""Phase 4C PostgreSQL publication-retention regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications import retention, services
from apps.publications.artifacts import cleanup_stale_artifacts
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path


@pytest.fixture
def retention_scope(db):
    admin = User.objects.create_user("retention@example.test", "Retention", "password")
    department = Department.objects.create(name="Retention", short_code="RET", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    scope = DatasetScopeState.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        source_revision=7,
        current_source_fingerprint="current-fingerprint",
    )
    return admin, department, scope


def _publication(*, department, scope, version, status, snapshot=None, usable=False):
    publication = DatasetPublication(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=scope.source_revision,
        source_fingerprint=f"fingerprint-{version}",
        source_snapshot={"version": version} if snapshot is None else snapshot,
        status=status,
        artifact_ready=usable,
        artifact_status=(
            DatasetPublication.ArtifactStatus.READY
            if usable
            else DatasetPublication.ArtifactStatus.PENDING
        ),
        artifact_path="",
    )
    if usable:
        publication.artifact_path = publication_artifact_relative_path(
            department_id=department.id, publication_id=publication.id
        )
        publication.artifact_size = 1
        publication.artifact_sha256 = "a" * 64
        publication.artifact_nonce = b"n" * 12
        publication.artifact_wrapped_cek = b"w" * 40
        publication.artifact_encryption_algorithm = "AES-256-GCM"
        publication.artifact_wrapping_algorithm = "AES-KW-RFC3394"
        publication.artifact_kek_version = "test"
        publication.artifact_signature = b"s" * 64
        publication.artifact_signature_algorithm = "Ed25519"
        publication.artifact_signing_key_version = "test"
    publication.save()
    return publication


def _age(publication, *, days):
    DatasetPublication.objects.filter(pk=publication.pk).update(
        created_at=timezone.now() - timedelta(days=days)
    )
    publication.refresh_from_db()


def _terminal_job(*, publication, status, days):
    completed = timezone.now() - timedelta(days=days)
    return PublicationJob.objects.create(
        department=publication.department,
        dataset_type_code=publication.dataset_type_code,
        scope_state=publication.scope_state,
        source_revision=publication.source_revision,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        status=status,
        build_publication=publication,
        completed_at=completed,
    )


@pytest.mark.django_db(transaction=True)
def test_retention_keeps_current_staged_building_and_two_newest_rollback_predecessors(
    retention_scope,
):
    _, department, scope = retention_scope
    older = _publication(
        department=department,
        scope=scope,
        version=17,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    second = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    newest = _publication(
        department=department,
        scope=scope,
        version=19,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    current = _publication(
        department=department,
        scope=scope,
        version=20,
        status=DatasetPublication.Status.PUBLISHED,
        usable=True,
    )
    staged = _publication(
        department=department, scope=scope, version=21, status=DatasetPublication.Status.STAGED
    )
    building = _publication(
        department=department, scope=scope, version=22, status=DatasetPublication.Status.BUILDING
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))

    result = retention.run_publication_retention()

    older.refresh_from_db()
    second.refresh_from_db()
    newest.refresh_from_db()
    current.refresh_from_db()
    staged.refresh_from_db()
    building.refresh_from_db()
    assert result["obsoleted"] == 1
    assert older.status == DatasetPublication.Status.OBSOLETE
    assert older.source_snapshot is None
    assert second.status == newest.status == DatasetPublication.Status.SUPERSEDED
    assert second.artifact_ready and newest.artifact_ready
    assert current.status == DatasetPublication.Status.PUBLISHED
    assert staged.status == DatasetPublication.Status.STAGED
    assert building.status == DatasetPublication.Status.BUILDING


@pytest.mark.django_db(transaction=True)
def test_retention_uses_completed_at_for_failed_and_cancelled_snapshot_expiry(retention_scope):
    _, department, scope = retention_scope
    failed_old = _publication(
        department=department, scope=scope, version=1, status=DatasetPublication.Status.FAILED
    )
    failed_recent = _publication(
        department=department, scope=scope, version=2, status=DatasetPublication.Status.FAILED
    )
    cancelled_old = _publication(
        department=department, scope=scope, version=3, status=DatasetPublication.Status.CANCELLED
    )
    _age(failed_old, days=40)
    _age(failed_recent, days=40)
    _age(cancelled_old, days=40)
    _terminal_job(publication=failed_old, status=PublicationJob.Status.FAILED, days=31)
    _terminal_job(publication=failed_recent, status=PublicationJob.Status.FAILED, days=29)
    _terminal_job(publication=cancelled_old, status=PublicationJob.Status.CANCELLED, days=31)

    result = retention.run_publication_retention()

    failed_old.refresh_from_db()
    failed_recent.refresh_from_db()
    cancelled_old.refresh_from_db()
    assert result["snapshots_purged"] == 2
    assert failed_old.status == DatasetPublication.Status.FAILED
    assert failed_old.source_snapshot is None
    assert failed_recent.source_snapshot == {"version": 2}
    assert cancelled_old.status == DatasetPublication.Status.CANCELLED
    assert cancelled_old.source_snapshot is None


@pytest.mark.django_db(transaction=True)
def test_clean_revert_cancelled_attempt_is_never_obsoleted(retention_scope):
    _, department, scope = retention_scope
    candidate = _publication(
        department=department,
        scope=scope,
        version=14,
        status=DatasetPublication.Status.CANCELLED,
    )
    _age(candidate, days=31)
    _terminal_job(publication=candidate, status=PublicationJob.Status.CANCELLED, days=31)

    retention.run_publication_retention()

    candidate.refresh_from_db()
    assert candidate.status == DatasetPublication.Status.CANCELLED
    assert candidate.source_snapshot is None
    assert candidate.source_fingerprint == "fingerprint-14"


@pytest.mark.django_db(transaction=True)
def test_retention_artifact_removal_runs_only_after_outer_transaction_commit(
    retention_scope, tmp_path
):
    _, department, scope = retention_scope
    candidate = _publication(
        department=department,
        scope=scope,
        version=17,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    _publication(
        department=department,
        scope=scope,
        version=19,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    artifact = tmp_path / candidate.artifact_path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"ciphertext")
    with override_settings(PUBLICATION_ARTIFACT_ROOT=tmp_path):
        with transaction.atomic():
            result = retention.run_publication_retention()
            assert result["obsoleted"] == 1
            assert artifact.exists()
        assert not artifact.exists()
    candidate.refresh_from_db()
    assert candidate.status == DatasetPublication.Status.OBSOLETE
    assert candidate.source_snapshot is None


@pytest.mark.django_db(transaction=True)
def test_artifact_cleanup_failure_remains_retryable(retention_scope, tmp_path, monkeypatch):
    _, department, scope = retention_scope
    candidate = _publication(
        department=department,
        scope=scope,
        version=17,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    _publication(
        department=department,
        scope=scope,
        version=19,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    artifact = tmp_path / candidate.artifact_path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"ciphertext")
    monkeypatch.setattr(
        services, "remove_artifact_path", lambda _: (_ for _ in ()).throw(OSError("busy"))
    )
    with override_settings(PUBLICATION_ARTIFACT_ROOT=tmp_path):
        retention.run_publication_retention()
        assert artifact.exists()
        monkeypatch.undo()
        assert cleanup_stale_artifacts() == 1
    assert not artifact.exists()


@pytest.mark.django_db(transaction=True)
def test_retention_is_bounded_idempotent_and_does_not_change_scope_source_state(retention_scope):
    _, department, scope = retention_scope
    candidates = [
        _publication(
            department=department,
            scope=scope,
            version=version,
            status=DatasetPublication.Status.SUPERSEDED,
            usable=True,
        )
        for version in range(1, 6)
    ]
    revision, fingerprint = scope.source_revision, scope.current_source_fingerprint

    first = retention.run_publication_retention(batch_size=1)
    second = retention.run_publication_retention(batch_size=100)
    third = retention.run_publication_retention(batch_size=100)

    scope.refresh_from_db()
    assert first["considered"] == 1
    assert first["obsoleted"] == 1
    assert second["obsoleted"] == 2
    assert third["obsoleted"] == 0
    assert scope.source_revision == revision
    assert scope.current_source_fingerprint == fingerprint
    assert (
        DatasetPublication.objects.filter(
            pk__in=[candidate.pk for candidate in candidates],
            status=DatasetPublication.Status.OBSOLETE,
        ).count()
        == 3
    )


@pytest.mark.django_db(transaction=True)
def test_retention_skips_candidate_that_becomes_current_before_authoritative_lock(retention_scope):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL lifecycle locking is required.")
    _, department, scope = retention_scope
    candidate = _publication(
        department=department,
        scope=scope,
        version=17,
        status=DatasetPublication.Status.SUPERSEDED,
        usable=True,
    )
    # Candidate discovery may be stale; the locked recheck protects an attempt
    # that has become authoritative before maintenance reaches it.
    scope.current_published_publication = candidate
    scope.save(update_fields=("current_published_publication",))

    result = retention._process_candidate(
        publication_id=candidate.id, now=timezone.now(), dry_run=False
    )

    candidate.refresh_from_db()
    assert result == "skipped"
    assert candidate.status == DatasetPublication.Status.SUPERSEDED


@pytest.mark.django_db(transaction=True)
def test_retention_purged_snapshot_is_not_offered_for_inspection(retention_scope, client):
    admin, department, scope = retention_scope
    current = _publication(
        department=department,
        scope=scope,
        version=13,
        status=DatasetPublication.Status.PUBLISHED,
        snapshot=None,
        usable=True,
    )
    current.source_snapshot = None
    current.save(update_fields=("source_snapshot",))
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    client.force_login(admin)

    response = client.get(f"/publications/scopes/{scope.id}/")

    assert response.status_code == 200
    assert b"Inspect changes" not in response.content
