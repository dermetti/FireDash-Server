"""Final document schema and hardened PostgreSQL guards."""

import pytest
from django.db.migrations.executor import MigrationExecutor

from tools.migration_catalog import disposable_database


@pytest.mark.django_db(transaction=True)
def test_document_baseline_installs_final_queue_and_guards():
    with disposable_database() as disposable:
        executor = MigrationExecutor(disposable)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        queue = executor.loader.project_state(targets).apps.get_model(
            "publications", "DocumentArtifactCleanup"
        )
        with disposable.cursor() as cursor:
            tables = disposable.introspection.table_names(cursor)
            assert "publications_documentartifactcleanup" in tables
            assert not any(
                "fireplan" in table for table in tables if table.startswith("publications_")
            )
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
