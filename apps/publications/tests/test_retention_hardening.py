"""Restore-aware policy, read-only audit and physical cleanup accounting."""

import io
import json
import os
import time
import uuid
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.publications import artifacts, document_artifacts, retention
from apps.publications.builders import build_source_payload
from apps.publications.document_v2 import build_document_v2_generation, build_document_v2_manifest
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    DocumentArtifactCleanup,
    PublicationJob,
)
from apps.publications.paths import document_artifact_relative_path
from apps.publications.registry import get_dataset_definition
from apps.publications.restore import select_restorable_predecessors
from apps.publications.tests import test_fire_plan_v2_delivery as delivery
from apps.publications.tests import test_retention as existing


@pytest.fixture
def retention_scope(db):
    return existing.retention_scope.__wrapped__(db)


@pytest.fixture
def v2_delivery_context(db, tmp_path, monkeypatch):
    yield from delivery.v2_delivery_context.__wrapped__(db, tmp_path, monkeypatch)


def _document(scope, version, *, valid=True, status="SUPERSEDED"):
    code = scope.dataset_type_code
    publication = DatasetPublication.objects.create(
        scope_state=scope,
        dataset_type_code=code,
        department_id=scope.department_id,
        station_id=scope.station_id,
        scope_type=scope.scope_type,
        version_number=version,
        schema_version=2,
        source_revision=1,
        status="BUILDING" if valid else status,
        source_snapshot=build_source_payload(
            definition=get_dataset_definition(code),
            department=scope.department,
            station=scope.station,
        )
        if valid
        else {"unneeded": "x" * 131072},
        artifact_status="PENDING" if valid else "READY",
        artifact_ready=not valid,
    )
    if valid:
        build_document_v2_generation(publication=publication)
        build_document_v2_manifest(publication=publication)
        publication.status, publication.artifact_status, publication.artifact_ready = (
            status,
            "READY",
            True,
        )
        publication.save()
    return publication


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "code", ["department_fire_plans", "department_klgv_plans", "department_files", "station_files"]
)
@pytest.mark.parametrize("valid_count", [0, 1, 3])
def test_restore_aware_policy_and_default_off(v2_delivery_context, code, valid_count):
    from apps.organizations.models import Station

    _, department, original, *_ = v2_delivery_context
    scope = (
        original
        if code == original.dataset_type_code
        else DatasetScopeState.objects.create(
            dataset_type_code=code,
            department=department,
            scope_type="STATION" if code == "station_files" else "DEPARTMENT",
            station=Station.objects.get(department=department) if code == "station_files" else None,
        )
    )
    valid = [_document(scope, version) for version in range(1, valid_count + 1)]
    invalid = [_document(scope, version, valid=False) for version in range(10, 29)]
    current = _document(scope, 40, status="PUBLISHED")
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    with CaptureQueriesContext(connection) as queries:
        selected = select_restorable_predecessors(scope=scope, limit=2)
    assert [p.id for p in selected] == [p.id for p in reversed(valid[-2:])]
    assert len(queries) <= 11  # bounded batches; no document-per-row queries
    print(f"{code} valid={valid_count}: {len(queries)} selection queries")
    assert all('"build_summary"' not in q["sql"] for q in queries)
    with override_settings(PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=False):
        assert retention._is_retained_predecessor(scope=scope, publication=invalid[-1])
        assert not retention._is_retained_predecessor(
            scope=scope, publication=valid[-1] if valid else invalid[0]
        )
    with override_settings(PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=True):
        assert not retention._is_retained_predecessor(scope=scope, publication=invalid[-1])
        result = retention.run_publication_retention(batch_size=100)
    assert result["obsoleted"] == len(invalid) + max(0, valid_count - 2)
    assert (
        DatasetPublication.objects.filter(scope_state=scope).count()
        == len(invalid) + valid_count + 1
    )
    current.refresh_from_db()
    assert current.status == "PUBLISHED" and current.source_snapshot is not None
    for publication in valid[-2:]:
        publication.refresh_from_db()
        assert publication.status == "SUPERSEDED"
    for publication in invalid:
        publication.refresh_from_db()
        assert publication.status == "OBSOLETE" and publication.source_snapshot is None


@pytest.mark.django_db(transaction=True)
def test_audit_is_read_only_deterministic_and_keyless(v2_delivery_context, tmp_path):
    _, _, scope, *_ = v2_delivery_context
    _document(scope, 1)
    _document(scope, 2, valid=False)
    _document(scope, 3, valid=False)
    current = _document(scope, 4, status="PUBLISHED")
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    PublicationJob.objects.create(
        scope_state=scope,
        department=scope.department,
        dataset_type_code=scope.dataset_type_code,
        source_revision=1,
        status="PENDING",
    )
    file = tmp_path / "untouched.bin"
    file.write_bytes(b"keep")
    DocumentArtifactCleanup.objects.create(artifact_id=uuid.uuid4(), artifact_path="untouched.bin")
    from apps.publications.models import DocumentGenerationKeyGrant

    installation = v2_delivery_context[4]
    grant = DocumentGenerationKeyGrant.objects.create(
        publication=current, app_installation=installation
    )
    grant_before = DocumentGenerationKeyGrant.objects.values().get(pk=grant.pk)
    before = list(DatasetPublication.objects.values())
    outputs = []
    with override_settings(
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "absent",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "absent",
    ):
        for _ in range(2):
            output = io.StringIO()
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                with CaptureQueriesContext(connection) as queries:
                    call_command("audit_publication_retention", json_output=True, stdout=output)
                assert not any("FOR UPDATE" in q["sql"] for q in queries)
            outputs.append(output.getvalue())
    assert outputs[0] == outputs[1]
    report = json.loads(outputs[0])
    row = report["scopes"][0]
    assert row["current_policy_protected"] == [3, 2]
    assert row["proposed_policy_protected"] == [1]
    assert row["newly_unprotected"] == [3, 2]
    assert row["cleanup_blocked_by_active_job"]
    assert row["policies_disagree"]
    assert "documents" not in outputs[0] and "wrapped_key" not in outputs[0]
    assert list(DatasetPublication.objects.values()) == before
    assert DocumentGenerationKeyGrant.objects.values().get(pk=grant.pk) == grant_before
    assert DocumentArtifactCleanup.objects.count() == PublicationJob.objects.count() == 1
    assert file.read_bytes() == b"keep"
    output = io.StringIO()
    call_command("audit_publication_retention", stdout=output)
    assert "CURRENT POLICY" in output.getvalue()
    assert "PROPOSED POLICY" in output.getvalue()
    assert "NOT EXECUTED" in output.getvalue()


@pytest.mark.django_db
def test_non_document_selection_never_reads_snapshot(retention_scope):
    _, department, scope = retention_scope
    for version in range(1, 26):
        existing._publication(
            department=department,
            scope=scope,
            version=version,
            status="SUPERSEDED",
            usable=True,
            snapshot={"large": "x" * 131072},
        )
    with CaptureQueriesContext(connection) as queries:
        selected = select_restorable_predecessors(scope=scope, limit=2)
    assert [p.version_number for p in selected] == [25, 24]
    assert len(queries) == 1
    assert '"source_snapshot"' not in queries[0]["sql"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("flag", [False, True])
def test_locked_recheck_protects_new_current_and_active_job(retention_scope, flag):
    _, department, scope = retention_scope
    publication = existing._publication(
        department=department, scope=scope, version=1, status="SUPERSEDED", usable=True
    )
    scope.current_published_publication = publication
    scope.save(update_fields=("current_published_publication",))
    with override_settings(PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=flag):
        assert (
            retention._process_candidate(
                publication_id=publication.id, now=timezone.now(), dry_run=False
            )
            == "skipped"
        )
        scope.current_published_publication = None
        scope.save(update_fields=("current_published_publication",))
        PublicationJob.objects.create(
            scope_state=scope,
            department=department,
            dataset_type_code=scope.dataset_type_code,
            source_revision=1,
            status="RUNNING",
        )
        assert (
            retention._process_candidate(
                publication_id=publication.id, now=timezone.now(), dry_run=False
            )
            == "skipped"
        )
    publication.refresh_from_db()
    assert publication.status == "SUPERSEDED" and publication.source_snapshot is not None


@pytest.mark.django_db(transaction=True)
def test_publication_cleanup_counts_deleted_missing_and_retry(retention_scope, tmp_path):
    _, department, scope = retention_scope
    publication = existing._publication(
        department=department, scope=scope, version=1, status="OBSOLETE", usable=True
    )
    path = tmp_path / publication.artifact_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ciphertext")
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path, PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / ".tmp"
    ):
        with patch("pathlib.Path.unlink", side_effect=PermissionError("busy")):
            failed = artifacts.cleanup_stale_artifacts()
        assert failed.cleanup_failed == failed.cleanup_deferred == 1
        assert failed.files_deleted == 0 and path.exists()
        first = artifacts.cleanup_stale_artifacts()
        second = artifacts.cleanup_stale_artifacts()
    assert first.files_deleted == 1 and first.files_already_missing == 0
    assert second.files_deleted == 0 and second.files_already_missing == 1
    publication.refresh_from_db()
    assert publication.artifact_path and publication.artifact_status == "READY"


@pytest.mark.django_db(transaction=True)
def test_document_queue_accounting_and_failures(tmp_path):
    identity = uuid.uuid4()
    relative = document_artifact_relative_path(artifact_id=identity)
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ciphertext")
    DocumentArtifactCleanup.objects.create(artifact_id=identity, artifact_path=relative)
    with override_settings(PUBLICATION_ARTIFACT_ROOT=tmp_path):
        with patch(
            "apps.publications.document_artifacts.remove_artifact_path", side_effect=OSError("busy")
        ):
            failed = document_artifacts.cleanup_unreferenced_document_artifacts()
        assert failed.cleanup_failed == failed.cleanup_deferred == 1
        assert DocumentArtifactCleanup.objects.count() == 1
        first = document_artifacts.cleanup_unreferenced_document_artifacts()
        assert first.files_deleted == first.document_cleanup_records_processed == 1
        DocumentArtifactCleanup.objects.create(artifact_id=identity, artifact_path=relative)
        second = document_artifacts.cleanup_unreferenced_document_artifacts()
        assert second.files_deleted == 0 and second.files_already_missing == 1
        assert second.document_cleanup_records_processed == 1
        assert not DocumentArtifactCleanup.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_temp_cleanup_and_unsafe_paths_are_separate(tmp_path):
    stale = tmp_path / ".tmp" / "old"
    stale.mkdir(parents=True)
    os.utime(stale, (time.time() - 120, time.time() - 120))
    outside = tmp_path / "keep.bin"
    outside.write_bytes(b"keep")
    root = tmp_path / "root"
    root.mkdir()
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=root,
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / ".tmp",
        PUBLICATION_ARTIFACT_STALE_SECONDS=60,
    ):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.remove_artifact_path("../keep.bin")
        result = artifacts.cleanup_stale_artifacts()
    assert result.temporary_entries_removed == 1 and result.files_deleted == 0
    assert outside.read_bytes() == b"keep"


@pytest.mark.django_db(transaction=True)
def test_retention_postcommit_deletions_are_in_maintenance_accounting(retention_scope, tmp_path):
    _, department, scope = retention_scope
    publication = existing._publication(
        department=department, scope=scope, version=1, status="SUPERSEDED", usable=True
    )
    path = tmp_path / publication.artifact_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ciphertext")
    with override_settings(
        PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS=0,
        PUBLICATION_ARTIFACT_ROOT=tmp_path,
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / ".tmp",
    ):
        output = io.StringIO()
        call_command("cleanup_orphan_artifacts", stdout=output)
        assert "files_deleted: 1" in output.getvalue()
        output = io.StringIO()
        call_command("cleanup_orphan_artifacts", stdout=output)
        assert "files_deleted: 0" in output.getvalue()
        assert "files_already_missing: 1" in output.getvalue()
    publication.refresh_from_db()
    assert publication.status == "OBSOLETE" and publication.artifact_path


@pytest.mark.django_db
@pytest.mark.parametrize("limit", [0, 1, 2])
def test_audit_mixed_scopes_empty_histories_and_configured_limit(retention_scope, limit):
    from apps.organizations.models import Station
    from apps.publications.tests.test_history_queries import _publication

    _, department, original = retention_scope
    station = Station.objects.create(department=department, name="Station", short_code="ST")
    scopes = [
        original,
        DatasetScopeState.objects.create(dataset_type_code="dangerous_goods", scope_type="SYSTEM"),
        DatasetScopeState.objects.create(
            dataset_type_code="station_personnel",
            scope_type="STATION",
            department=department,
            station=station,
        ),
    ]
    for scope in scopes:
        for version in (1, 2, 3):
            _publication(
                department=department if scope.department_id else None,
                scope=scope,
                version_number=version,
                status="SUPERSEDED",
            )
    DatasetScopeState.objects.create(
        dataset_type_code="department_phonebook", department=department
    )
    with override_settings(PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS=limit):
        output = io.StringIO()
        call_command("audit_publication_retention", json_output=True, stdout=output)
    report = json.loads(output.getvalue())
    assert len(report["scopes"]) == 4
    for row in report["scopes"]:
        expected = [3, 2][:limit] if row["candidates"] else []
        assert row["current_policy_protected"] == row["proposed_policy_protected"] == expected
        assert not row["policies_disagree"]


@pytest.mark.django_db(transaction=True)
def test_locked_recheck_reprotects_newly_eligible_predecessor(retention_scope):
    _, department, scope = retention_scope
    older = existing._publication(
        department=department, scope=scope, version=1, status="SUPERSEDED", usable=True
    )
    newer = [
        existing._publication(
            department=department, scope=scope, version=v, status="SUPERSEDED", usable=True
        )
        for v in (2, 3)
    ]
    with override_settings(PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=True):
        assert older.id in retention._candidate_ids(limit=100, now=timezone.now())
        # A serialized lifecycle operation retires a newer predecessor before processing.
        newer[0].status = "OBSOLETE"
        newer[0].save(update_fields=("status",))
        assert (
            retention._process_candidate(publication_id=older.id, now=timezone.now(), dry_run=False)
            == "skipped"
        )
    older.refresh_from_db()
    assert older.status == "SUPERSEDED" and older.source_snapshot is not None


def test_explicit_boolean_flag_parsing(monkeypatch):
    from config.settings.env import get_bool

    name = "PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED"
    monkeypatch.delenv(name, raising=False)
    assert get_bool(name, default=False) is False
    for value in ("false", "0", "off"):
        monkeypatch.setenv(name, value)
        assert get_bool(name, default=False) is False
    monkeypatch.setenv(name, "true")
    assert get_bool(name, default=False) is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("file_exists", [False, True])
def test_nonready_terminal_cleanup_preserves_legacy_path_clearing(
    retention_scope, tmp_path, file_exists
):
    from apps.publications.paths import publication_artifact_relative_path

    _, department, scope = retention_scope
    publication = existing._publication(
        department=department, scope=scope, version=1, status="FAILED"
    )
    publication.artifact_path = publication_artifact_relative_path(
        department_id=department.id, publication_id=publication.id
    )
    publication.save(update_fields=("artifact_path",))
    path = tmp_path / publication.artifact_path
    if file_exists:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"ciphertext")
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path, PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / ".tmp"
    ):
        result = artifacts.cleanup_stale_artifacts()
    assert result.files_deleted == int(file_exists)
    assert result.files_already_missing == int(not file_exists)
    assert result.cleanup_failed == 0
    publication.refresh_from_db()
    assert publication.artifact_path == "" and publication.status == "FAILED"


@pytest.mark.django_db(transaction=True)
def test_stage_a_orphan_accounting_keeps_failed_physical_cleanup_durable(v2_delivery_context):
    from apps.publications.document_v2 import get_or_create_document_artifact
    from apps.publications.models import DocumentArtifact

    artifact, _ = get_or_create_document_artifact(
        dataset_type_code="department_fire_plans",
        canonical_document_id=uuid.uuid4(),
        sanitized_pdf=b"disposable PDF fixture",
    )
    path = settings.PUBLICATION_ARTIFACT_ROOT / artifact.artifact_path
    with patch(
        "apps.publications.document_artifacts.remove_artifact_path",
        side_effect=PermissionError("busy"),
    ):
        result = document_artifacts.cleanup_unreferenced_document_artifacts()
    assert result.document_orphan_records_removed == 1
    assert result.document_cleanup_records_processed == result.files_deleted == 0
    assert result.cleanup_failed == result.cleanup_deferred == 1
    assert not DocumentArtifact.objects.filter(pk=artifact.id).exists()
    assert DocumentArtifactCleanup.objects.filter(artifact_id=artifact.id).exists()
    assert path.exists()
    retry = document_artifacts.cleanup_unreferenced_document_artifacts()
    assert retry.files_deleted == retry.document_cleanup_records_processed == 1
    assert retry.document_orphan_records_removed == 0
    assert not path.exists() and not DocumentArtifactCleanup.objects.exists()
