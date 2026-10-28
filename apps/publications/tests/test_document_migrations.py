"""Disposable PostgreSQL upgrade and fresh-chain coverage for Phase 2."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_fresh_chain_and_cleanup_queue_rename_preserve_data(disposable_migration_database):
    assert connection.vendor == "postgresql"
    disposable = disposable_migration_database
    executor = MigrationExecutor(disposable)
    latest = executor.loader.graph.leaf_nodes()
    phase1 = [
        (app, "0031_unified_document_generation") if app == "publications" else (app, migration)
        for app, migration in latest
    ]
    executor.migrate(phase1)
    historical = executor.loader.project_state(phase1).apps
    old_queue = historical.get_model("publications", "FirePlanDocumentArtifactCleanup")
    artifact_id = uuid.uuid4()
    row = old_queue.objects.create(
        artifact_id=artifact_id, artifact_path=f"documents/{artifact_id}/artifact.bin"
    )
    executor = MigrationExecutor(disposable)
    executor.migrate(latest)
    current = executor.loader.project_state(latest).apps
    queue = current.get_model("publications", "DocumentArtifactCleanup")
    saved = queue.objects.get(pk=row.pk)
    assert (saved.artifact_id, saved.artifact_path, saved.created_at) == (
        row.artifact_id,
        row.artifact_path,
        row.created_at,
    )
    with disposable.cursor() as cursor:
        tables = disposable.introspection.table_names(cursor)
        assert "publications_documentartifactcleanup" in tables
        assert not any("fireplan" in table for table in tables if table.startswith("publications_"))
        cursor.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace=current_schema()::regnamespace "
            "AND proname LIKE 'publications_%'"
        )
        functions = {row[0] for row in cursor.fetchall()}
        assert not any("fire_plan" in name for name in functions)
        assert {
            "publications_document_generation_complete",
            "publications_guard_document_generation",
            "publications_guard_document_reference",
            "publications_guard_document_crypto_delete",
        } <= functions
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'publications_document_generation_complete(publications_datasetpublication)'::regprocedure)"
        )
        definition = cursor.fetchone()[0]
        assert "jsonb_typeof(d->'generation_wrapped_cek')='string'" in definition
        assert "THEN 'path'" not in definition
        cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        assert not any("fire_plan" in row[0] for row in cursor.fetchall())
    assert queue._meta.get_field("artifact_id").unique
    assert queue._meta.get_field("artifact_path").unique
