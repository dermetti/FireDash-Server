"""Exercise real replacement bookkeeping against isolated PostGIS schemas."""

import importlib
import io
import re
from collections import Counter
from itertools import product
from uuid import uuid4

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tools.migration_catalog import (
    catalog,
    disposable_database,
    historical_executor,
    normalized_schema_dump,
    schema_dump,
    schema_dump_available,
)


@pytest.mark.django_db(transaction=True)
def test_normalized_postgresql_schema_dumps_match():
    if not schema_dump_available():
        pytest.skip("pg_dump unavailable locally; CI uses the PostgreSQL 17 service client")
    dumps = []
    for historical in (True, False):
        with disposable_database() as db:
            migrate_all(db, historical=historical)
            dumps.append(normalized_schema_dump(schema_dump(db), catalog(db)))
    assert dumps[0] == dumps[1]


def migrate_all(db, *, historical=False):
    executor = historical_executor(db) if historical else MigrationExecutor(db)
    targets = executor.loader.graph.leaf_nodes()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def assert_seeds(state):
    from apps.publications.registry import DATASET_REGISTRY

    rows = state.get_model("publications", "DatasetTypeRegistry").objects
    assert rows.count() == len(DATASET_REGISTRY) - 1
    for definition in DATASET_REGISTRY.values():
        if definition.code == "test_system_dataset":
            continue
        row = rows.get(code=definition.code)
        assert (
            row.scope,
            row.current_schema_version,
            row.supported_schema_versions,
            row.required,
            row.feature_code,
        ) == (
            definition.scope,
            definition.current_schema_version,
            list(definition.supported_schema_versions),
            definition.required,
            definition.feature_code,
        )
    scope = state.get_model("publications", "DatasetScopeState").objects.get(
        dataset_type_code="dangerous_goods"
    )
    assert (scope.scope_type, scope.department_id, scope.station_id) == ("SYSTEM", None, None)
    mail = state.get_model("outbound_mail", "SystemMailConfiguration").objects.get(singleton=True)
    assert mail.delivery_mode == "DISABLED"
    rescue = state.get_model("authorization", "VehicleRescueGuidesConfiguration").objects.get(
        singleton=True
    )
    assert (rescue.provider, rescue.mode, rescue.web_url) == (
        "euro_rescue",
        "web",
        "https://rescue.euroncap.com/",
    )


def assert_semantic_catalogs(reference, fresh, db):
    for key in ("columns", "functions", "triggers", "ownership"):
        assert reference[key] == fresh[key], key

    # PostgreSQL keeps automatic names across RenameField/RenameModel. Compare
    # index definitions independently of those names, retaining multiplicity.
    def indexes(rows):
        return Counter(
            (table, re.sub(r"INDEX \S+ ON", "INDEX <name> ON", definition))
            for table, _name, definition in rows
        )

    assert indexes(reference["indexes"]) == indexes(fresh["indexes"])
    old = {(row[0], row[1]): row for row in reference["constraints"]}
    new = {(row[0], row[1]): row for row in fresh["constraints"]}
    lifecycle = {
        ("authorization_departmentmembership", "department_membership_lifecycle_provenance"),
        ("authorization_stationadminassignment", "station_assignment_lifecycle_provenance"),
    }
    # Check every lifecycle combination, including unrecognized statuses and NULL.
    for lifecycle_key in lifecycle:
        left, right = old.pop(lifecycle_key), new.pop(lifecycle_key)
        assert left[:3] == right[:3] and left[4:] == right[4:]
        with db.cursor() as cursor:
            for status, revoked_at, revoked_by in product(
                ["ACTIVE", "SUSPENDED", "REVOKED", "invalid", None],
                [None, timezone.now()],
                [None, uuid4()],
            ):
                cursor.execute(
                    f"SELECT {left[3][6:]}, {right[3][6:]} FROM "
                    "(SELECT %s::varchar AS status, %s::timestamptz AS revoked_at, "
                    "%s::uuid AS revoked_by_id) v",
                    [status, revoked_at, revoked_by],
                )
                assert len(set(cursor.fetchone())) == 1

    # Unique/PK constraint names inherit table and column renames as well.
    def constraints(rows):
        return Counter((row[0], *row[2:]) for row in rows.values())

    assert constraints(old) == constraints(new)


def make_records(state):
    get = state.get_model
    user = get("accounts", "User").objects.create(
        email="baseline@example.test", display_name="Test"
    )
    department = get("organizations", "Department").objects.create(
        name="Preserved", short_code="BETA", created_by_id=user.pk
    )
    get("file_library", "DepartmentQuotaPolicy").objects.create(
        department_id=department.pk, department_files_bytes=123456789, station_files_bytes=987654321
    )
    scope = get("publications", "DatasetScopeState").objects.create(
        scope_type="DEPARTMENT",
        department_id=department.pk,
        dataset_type_code="department_hydrants",
    )
    publication = get("publications", "DatasetPublication").objects.create(
        scope_state_id=scope.pk,
        scope_type="DEPARTMENT",
        department_id=department.pk,
        dataset_type_code="department_hydrants",
        version_number=1,
        schema_version=1,
        source_revision=0,
        status="STAGED",
        artifact_kek_version="preserved-test-selector",
        artifact_signing_key_version="preserved-test-signing-selector",
    )
    get("audit", "AuditEvent").objects.create(
        actor_user_id=user.pk,
        department_id=department.pk,
        action="baseline.fixture",
        target_type="dataset_publication",
        target_uuid=publication.pk,
        request_id=uuid4(),
    )
    get("publications", "DocumentArtifactCleanup").objects.create(
        artifact_id=uuid4(), artifact_path="test/preserved-cleanup.bin"
    )
    # Deliberately retain an operator value that differs from the default seed.
    get("authorization", "VehicleRescueGuidesConfiguration").objects.update(
        web_url="https://rescue.euroncap.com/operator-setting"
    )
    get("outbound_mail", "SystemMailConfiguration").objects.update(smtp_host="operator.invalid")
    return user, department, publication


def business_fingerprints(db):
    """Compare complete synthetic records without emitting their contents."""
    result = {}
    tables = db.introspection.table_names()
    with db.cursor() as cursor:
        for table in tables:
            if table == "django_migrations" or table == "reference_data_hydrantimportpreview":
                continue
            cursor.execute(
                f"SELECT count(*), md5(string_agg(row_to_json(t)::text, '' "
                f"ORDER BY row_to_json(t)::text)) FROM {db.ops.quote_name(table)} t"
            )
            result[table] = cursor.fetchone()
    return result


def test_replacement_graph_and_user_root():
    loader = MigrationLoader(None)
    loader.graph.ensure_not_cyclic()
    replacements = loader.replacements
    assert len(replacements) == 16
    all_replaced = [key for migration in replacements.values() for key in migration.replaces]
    assert len(all_replaced) == len(set(all_replaced)) == 104
    original = historical_executor(None).loader
    app_labels = {
        config.label for config in apps.get_app_configs() if config.name.startswith("apps.")
    }
    assert set(all_replaced) == {key for key in original.disk_migrations if key[0] in app_labels}
    root = loader.graph.root_nodes("accounts")
    assert root == [("accounts", "0001_squashed_0002_beta")]
    assert any(
        op.__class__.__name__ == "CreateModel" and op.name == "User"
        for op in loader.graph.nodes[root[0]].operations
    )
    assert not any(key in loader.graph.nodes for key in all_replaced)


@pytest.mark.django_db(transaction=True)
def test_fresh_baseline_matches_historical_final_schema():
    with disposable_database() as db:
        state = migrate_all(db, historical=True)
        assert_seeds(state)
        reference = catalog(db)
    with disposable_database() as db:
        state = migrate_all(db)
        assert_seeds(state)
        fresh = catalog(db)
        assert_semantic_catalogs(reference, fresh, db)
        tables = set(db.introspection.table_names())
        assert "reference_data_hydrantimportpreview" not in tables
        assert not any("fireplan" in t for t in tables if t.startswith("publications_"))
        assert {
            "file_library_capacityreview",
            "file_library_storageimpact",
            "outbound_mail_tabletreportdeliveryrequest",
        } <= tables
        call_command("makemigrations", check=True, dry_run=True, stdout=io.StringIO())
        call_command("migrate", check_unapplied=True, verbosity=0)
        executor = MigrationExecutor(db)
        assert not executor.migration_plan(executor.loader.graph.leaf_nodes())
        recorded = MigrationRecorder(db).applied_migrations()
        assert set(executor.loader.replacements) <= recorded.keys()
        # Django records both the replacement and its original names even when
        # only replacement operations ran. These are normal framework records.
        assert all(
            key in recorded
            for migration in executor.loader.replacements.values()
            for key in migration.replaces
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("pending_cleanup", [False, True])
def test_established_server_preserves_records_and_records_replacements(pending_cleanup):
    with disposable_database() as db:
        executor = historical_executor(db)
        targets = executor.loader.graph.leaf_nodes()
        if pending_cleanup:
            targets = [
                (app, "0014_phonebook_entries") if app == "reference_data" else (app, name)
                for app, name in targets
            ]
        executor.migrate(targets)
        state = executor.loader.project_state(targets).apps
        make_records(state)
        before = business_fingerprints(db)
        schema = catalog(db)
        executor = MigrationExecutor(db)
        executor.loader.check_consistent_history(db)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        assert [(m.app_label, m.name, backwards) for m, backwards in plan] == (
            [("reference_data", "0015_remove_retired_hydrant_preview", False)]
            if pending_cleanup
            else []
        )
        with CaptureQueriesContext(db) as queries:
            executor.migrate(executor.loader.graph.leaf_nodes())
        assert not any(re.match(r"\s*(CREATE|ALTER)\s", q["sql"], re.I) for q in queries)
        assert business_fingerprints(db) == before
        after_schema = catalog(db)
        if pending_cleanup:
            for key in ("columns", "constraints", "indexes", "ownership"):
                schema[key] = [
                    row for row in schema[key] if row[0] != "reference_data_hydrantimportpreview"
                ]
        assert after_schema == schema
        executor = MigrationExecutor(db)
        assert (
            set(executor.loader.replacements) <= MigrationRecorder(db).applied_migrations().keys()
        )
        executor.loader.check_consistent_history(db)
        assert not executor.migration_plan(executor.loader.graph.leaf_nodes())
        executor.migrate(executor.loader.graph.leaf_nodes())
        assert business_fingerprints(db) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("blocker", ["rows", "dependent_view"])
def test_preview_cleanup_fails_closed(blocker):
    with disposable_database() as db:
        executor = historical_executor(db)
        targets = [
            (app, "0014_phonebook_entries") if app == "reference_data" else (app, name)
            for app, name in executor.loader.graph.leaf_nodes()
        ]
        executor.migrate(targets)
        state = executor.loader.project_state(targets).apps
        user, department, _publication = make_records(state)
        preview = state.get_model("reference_data", "HydrantImportPreview")
        if blocker == "rows":
            preview.objects.create(
                department_id=department.pk,
                created_by_id=user.pk,
                normalized_features=[{"synthetic": True}],
                expires_at=timezone.now(),
            )
        else:
            with db.cursor() as cursor:
                cursor.execute(
                    "CREATE VIEW test_preview_dependency AS SELECT id FROM "
                    "reference_data_hydrantimportpreview"
                )
        before = business_fingerprints(db)
        schema = catalog(db)
        executor = MigrationExecutor(db)
        error = RuntimeError if blocker == "rows" else DatabaseError
        with pytest.raises(error) as caught:
            executor.migrate(executor.loader.graph.leaf_nodes())
        if blocker == "rows":
            assert str(caught.value) == (
                "Refusing to drop reference_data_hydrantimportpreview: "
                "1 rows require explicit disposition."
            )
            assert preview.objects.count() == 1
        assert catalog(db) == schema
        assert business_fingerprints(db) == before
        assert ("reference_data", "0015_remove_retired_hydrant_preview") not in (
            MigrationRecorder(db).applied_migrations()
        )


@pytest.mark.django_db(transaction=True)
def test_fresh_guards_reject_mutations_and_cross_tenant_scope():
    with disposable_database() as db:
        state = migrate_all(db)
        user, department, publication = make_records(state)
        for statement in (
            "UPDATE audit_event SET action='changed'",
            "DELETE FROM audit_event",
            "TRUNCATE audit_event",
        ):
            with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
                with db.cursor() as cursor:
                    cursor.execute(statement)
        other = state.get_model("organizations", "Department").objects.create(
            name="Other", short_code="OTHER", created_by_id=user.pk
        )
        station = state.get_model("organizations", "Station").objects.create(
            department_id=other.pk, name="Other station", short_code="OTHER"
        )
        with pytest.raises(DatabaseError, match="Station must belong"), transaction.atomic():
            state.get_model("publications", "DatasetScopeState").objects.create(
                department_id=department.pk,
                station_id=station.pk,
                scope_type="STATION",
                dataset_type_code="station_personnel",
            )
        library = state.get_model("file_library", "Library").objects.create(
            department_id=department.pk
        )
        content = state.get_model("file_library", "Content").objects.create(
            library_id=library.pk, storage_key="test/immutable", size_bytes=1, sha256="a" * 64
        )
        with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
            state.get_model("file_library", "Content").objects.filter(pk=content.pk).update(
                size_bytes=2
            )
        activation = state.get_model("publications", "PublicationActivation").objects.create(
            scope_state_id=publication.scope_state_id,
            publication_id=publication.pk,
            activated_by_id=user.pk,
        )
        with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
            state.get_model("publications", "PublicationActivation").objects.filter(
                pk=activation.pk
            ).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            state.get_model("publications", "DatasetPublication").objects.filter(
                pk=publication.pk
            ).update(status="PUBLISHED", artifact_status="READY")


@pytest.mark.django_db(transaction=True)
def test_initializers_are_idempotent_and_preserve_operator_values():
    with disposable_database() as db:
        state = migrate_all(db)
        make_records(state)
        before = business_fingerprints(db)
        seeds = (
            ("authorization", "0001_squashed_0007_beta", "initialize_rescue_guides"),
            ("outbound_mail", "0001_squashed_0006_beta", "initialize_system_mail"),
            ("publications", "0001_squashed_0011_beta", "initialize_beta_registry"),
        )
        with db.schema_editor() as editor:
            for app, migration, name in seeds:
                function = getattr(
                    importlib.import_module(f"apps.{app}.migrations.{migration}"), name
                )
                function(state, editor)
                function(state, editor)
        assert business_fingerprints(db) == before
