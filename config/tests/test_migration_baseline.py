"""Permanent beta schema and accepted Phase 1 upgrade compatibility."""

import hashlib
import importlib
import io
import json
import re
from pathlib import Path
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
    normalized_schema_dump,
    schema_dump,
    schema_dump_available,
)


@pytest.mark.django_db(transaction=True)
def test_normalized_postgresql_schema_dumps_match():
    if not schema_dump_available():
        pytest.skip("pg_dump unavailable locally; CI uses the PostgreSQL 17 service client")
    dumps = []
    for phase_one in (True, False):
        with disposable_database() as db:
            migrate_all(db, phase_one=phase_one, historical=phase_one)
            if phase_one:
                migrate_all(db)
            dumps.append(normalized_schema_dump(schema_dump(db)))
    assert dumps[0] == dumps[1]


HISTORY_DIR = Path(__file__).resolve().parents[2] / "docs" / "migrations"
PHASE1_MAPPING = json.loads(
    (HISTORY_DIR / "beta-baseline-mapping.json").read_text(encoding="utf-8")
)
HISTORICAL_MIGRATIONS = {tuple(name.split(".", 1)) for name in PHASE1_MAPPING}
APPROVED_FORWARD_MIGRATIONS = {("tablets", "0010_installation_credential_index")}
TABLET_INDEXES = {
    (
        "tablets_appinstallation",
        "tablets_appinstallation_credential_hash_c0a9352e",
        "CREATE INDEX tablets_appinstallation_credential_hash_c0a9352e "
        "ON public.tablets_appinstallation USING btree (credential_hash)",
    ),
    (
        "tablets_appinstallation",
        "tablets_appinstallation_credential_hash_c0a9352e_like",
        "CREATE INDEX tablets_appinstallation_credential_hash_c0a9352e_like "
        "ON public.tablets_appinstallation USING btree (credential_hash varchar_pattern_ops)",
    ),
}


def historical_targets(loader):
    # Pin the accepted schema independently of later approved graph leaves.
    return [
        key
        for key in HISTORICAL_MIGRATIONS
        if not any(
            child.key in HISTORICAL_MIGRATIONS for child in loader.graph.node_map[key].children
        )
    ] + [
        key
        for key in loader.graph.leaf_nodes()
        if key[0] not in {k[0] for k in HISTORICAL_MIGRATIONS}
    ]


def assert_only_credential_indexes_added(before, after):
    assert set(after["indexes"]) - set(before["indexes"]) == TABLET_INDEXES
    assert set(before["indexes"]) <= set(after["indexes"])
    assert {
        **after,
        "indexes": [row for row in after["indexes"] if row not in TABLET_INDEXES],
    } == before


def index_ownership(db):
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, pg_get_userbyid(c.relowner), c.relacl::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='i' "
            "AND c.relname NOT LIKE 'django_migrations%' ORDER BY c.relname"
        )
        return {name: (owner, acl) for name, owner, acl in cursor.fetchall()}


def recorder_rows(db):
    return list(
        MigrationRecorder(db)
        .migration_qs.order_by("id")
        .values_list("id", "app", "name", "applied")
    )


class AcceptedPhaseOneLoader(MigrationLoader):
    """Reconstruct accepted replacement metadata for simulation only.

    Source hashes pin every operation, initializer and dependency to the accepted
    release. Django itself creates all recorder entries through normal migrate.
    No historical migration modules or manual recorder writes are needed.
    """

    def load_disk(self):
        super().load_disk()
        for key, migration in list(self.disk_migrations.items()):
            names = PHASE1_MAPPING.get(".".join(key))
            if names is not None:
                accepted_class = type(
                    "AcceptedPhaseOneMigration",
                    (migration.__class__,),
                    {"replaces": [tuple(name.split(".", 1)) for name in names]},
                )
                self.disk_migrations[key] = accepted_class(migration.name, migration.app_label)


def phase_one_executor(db):
    executor = MigrationExecutor(db)
    executor.loader = AcceptedPhaseOneLoader(db)
    return executor


def migrate_all(db, *, phase_one=False, historical=False):
    executor = phase_one_executor(db) if phase_one else MigrationExecutor(db)
    targets = (
        historical_targets(executor.loader) if historical else executor.loader.graph.leaf_nodes()
    )
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


def make_records(state):
    get = state.get_model
    user = get("accounts", "User").objects.create(
        email="baseline@example.test", display_name="Test"
    )
    department = get("organizations", "Department").objects.create(
        name="Preserved", short_code="BETA", created_by_id=user.pk
    )
    tablet = get("tablets", "Tablet").objects.create(
        department_id=department.pk, display_name="Preserved tablet"
    )
    now = timezone.now()
    get("tablets", "AppInstallation").objects.create(
        tablet_id=tablet.pk,
        installation_uuid=uuid4(),
        credential_hash="a" * 64,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"synthetic-public-key",
        hpke_ciphersuite="synthetic-suite",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now,
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


def test_permanent_graph_and_accepted_source_are_unchanged():
    loader = MigrationLoader(None)
    loader.graph.ensure_not_cyclic()
    assert not loader.replacements
    app_labels = {
        config.label for config in apps.get_app_configs() if config.name.startswith("apps.")
    }
    own = {key for key in loader.disk_migrations if key[0] in app_labels}
    assert own == HISTORICAL_MIGRATIONS | APPROVED_FORWARD_MIGRATIONS
    assert len(HISTORICAL_MIGRATIONS) == 16
    assert loader.disk_migrations[
        ("tablets", "0010_installation_credential_index")
    ].dependencies == [("tablets", "0003_squashed_0009_beta")]
    hashes = json.loads((HISTORY_DIR / "beta-phase1-source-hashes.json").read_text())
    assert set(hashes) == set(PHASE1_MAPPING)
    for app, name in HISTORICAL_MIGRATIONS:
        path = HISTORY_DIR.parents[1] / "apps" / app / "migrations" / f"{name}.py"
        source = path.read_text(encoding="utf-8")
        assert hashlib.sha256(source.encode()).hexdigest() == hashes[f"{app}.{name}"]
        assert "replaces =" not in source
    files = list((HISTORY_DIR.parents[1] / "apps").glob("*/migrations/[0-9]*.py"))
    assert {(path.parent.parent.name, path.stem) for path in files} == own
    root = loader.graph.root_nodes("accounts")
    assert root == [("accounts", "0001_squashed_0002_beta")]
    assert any(
        op.__class__.__name__ == "CreateModel" and op.name == "User"
        for op in loader.graph.nodes[root[0]].operations
    )


@pytest.mark.django_db(transaction=True)
def test_fresh_baseline_matches_accepted_phase_one_schema():
    with disposable_database() as db:
        state = migrate_all(db, phase_one=True, historical=True)
        assert_seeds(state)
        reference = catalog(db)
    with disposable_database() as db:
        state = migrate_all(db, historical=True)
        assert_seeds(state)
        fresh = catalog(db)
        assert fresh == reference
        evidence = json.loads((HISTORY_DIR / "beta-postgresql16-reference.json").read_text())
        assert {key: len(rows) for key, rows in fresh.items()} == evidence["catalog_counts"]
        state = migrate_all(db)
        assert_only_credential_indexes_added(fresh, catalog(db))
        tables = set(db.introspection.table_names())
        assert "reference_data_hydrantimportpreview" not in tables
        assert not any("fireplan" in t for t in tables if t.startswith("publications_"))
        assert {
            "file_library_capacityreview",
            "file_library_storageimpact",
            "outbound_mail_tabletreportdeliveryrequest",
        } <= tables
        for model in state.get_models():
            if model._meta.managed and not model._meta.proxy:
                assert model._meta.db_table in tables
                with db.cursor() as cursor:
                    columns = {
                        column.name
                        for column in db.introspection.get_table_description(
                            cursor, model._meta.db_table
                        )
                    }
                assert {field.column for field in model._meta.local_fields} <= columns
        call_command("makemigrations", check=True, dry_run=True, stdout=io.StringIO())
        call_command("migrate", check_unapplied=True, verbosity=0)
        executor = MigrationExecutor(db)
        assert not executor.migration_plan(executor.loader.graph.leaf_nodes())
        recorded = MigrationRecorder(db).applied_migrations()
        assert {tuple(name.split(".", 1)) for name in PHASE1_MAPPING} <= recorded.keys()
        originals = {
            tuple(name.split(".", 1)) for names in PHASE1_MAPPING.values() for name in names
        }
        assert not originals.intersection(recorded)
        # Independent evidence from the original-history reference pins security SQL.
        assert [
            (name, args, hashlib.sha256(definition.encode()).hexdigest())
            for name, args, definition, *_ in fresh["functions"]
        ] == [
            (row["name"], row["arguments"], row["definition_sha256"])
            for row in evidence["functions"]
        ]


@pytest.mark.django_db(transaction=True)
def test_established_phase_one_forward_upgrade_preserves_schema_records_and_history():
    with disposable_database() as db:
        migrate_all(db)
        fresh_current = catalog(db)
    with disposable_database() as db:
        state = migrate_all(db, phase_one=True, historical=True)
        make_records(state)
        before = business_fingerprints(db)
        schema = catalog(db)
        owners = index_ownership(db)
        records = MigrationRecorder(db).applied_migrations()
        historical_rows = recorder_rows(db)
        expected = {tuple(name.split(".", 1)) for name in PHASE1_MAPPING}
        expected.update(
            tuple(name.split(".", 1)) for names in PHASE1_MAPPING.values() for name in names
        )
        assert expected <= records.keys()
        assert not APPROVED_FORWARD_MIGRATIONS.intersection(records)
        executor = MigrationExecutor(db)
        executor.loader.check_consistent_history(db)
        targets = executor.loader.graph.leaf_nodes()
        assert [
            (migration.app_label, migration.name, backwards)
            for migration, backwards in executor.migration_plan(targets)
        ] == [("tablets", "0010_installation_credential_index", False)]
        executor.migrate(targets)
        upgraded = catalog(db)
        assert_only_credential_indexes_added(schema, upgraded)
        assert upgraded == fresh_current
        current_owners = index_ownership(db)
        assert {name: current_owners[name] for name in owners} == owners
        assert current_owners.keys() - owners.keys() == {row[1] for row in TABLET_INDEXES}
        table_owner, table_acl = next(
            (owner, acl)
            for name, _, owner, acl in upgraded["ownership"]
            if name == "tablets_appinstallation"
        )
        assert all(current_owners[row[1]] == (table_owner, table_acl) for row in TABLET_INDEXES)
        assert business_fingerprints(db) == before
        current_records = MigrationRecorder(db).applied_migrations()
        assert current_records.keys() == records.keys() | APPROVED_FORWARD_MIGRATIONS
        assert all(current_records[key] == value for key, value in records.items())
        current_rows = recorder_rows(db)
        assert current_rows[: len(historical_rows)] == historical_rows
        schema, records = upgraded, current_records
        for _ in range(2):
            executor = MigrationExecutor(db)
            executor.loader.check_consistent_history(db)
            targets = executor.loader.graph.leaf_nodes()
            assert not executor.migration_plan(targets)
            with CaptureQueriesContext(db) as queries:
                executor.migrate(targets)
            assert not any(
                re.match(r"\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\s", q["sql"], re.I)
                for q in queries
            )
            assert business_fingerprints(db) == before
            assert catalog(db) == schema
            assert MigrationRecorder(db).applied_migrations() == records
            assert recorder_rows(db) == current_rows


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
