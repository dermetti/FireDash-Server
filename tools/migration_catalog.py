"""Non-sensitive PostgreSQL catalog snapshots for migration transition tests."""

import hashlib
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import sqlparse
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from psycopg import sql


def schema_dump_available():
    return bool(os.environ.get("FIREDASH_TEST_PG_DUMP_CONTAINER") or shutil.which("pg_dump"))


def schema_dump(connection):
    """Dump disposable schema only, using PG17's client in the CI service."""
    if not connection.settings_dict["NAME"].startswith("test_baseline_"):
        raise ValueError("Schema dumps are restricted to disposable baseline databases")
    config = connection.settings_dict
    container = os.environ.get("FIREDASH_TEST_PG_DUMP_CONTAINER")
    prefix = ["docker", "exec", "-e", "PGPASSWORD", container] if container else []
    command = [
        *prefix,
        "pg_dump",
        "--schema-only",
        "--no-tablespaces",
        "--no-security-labels",
        "--exclude-table=public.django_migrations",
        "--host",
        "127.0.0.1" if container else config["HOST"],
        "--port",
        "5432" if container else str(config["PORT"] or 5432),
        "--username",
        config["USER"],
        "--dbname",
        config["NAME"],
    ]
    result = subprocess.run(
        command,
        env={**os.environ, "PGPASSWORD": config["PASSWORD"]},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        # Never include process environments or connection diagnostics in a test failure.
        raise RuntimeError(f"Disposable schema-only pg_dump failed (exit {result.returncode})")
    return result.stdout


def normalized_schema_dump(dump, snapshot):
    """Keep all DDL, normalizing only names, column order and AND operand order."""
    names = {}
    for _table, name, definition in snapshot["indexes"]:
        semantic = re.sub(r"INDEX \S+ ON", "INDEX <name> ON", definition)
        names[name] = "idx_" + hashlib.sha256(semantic.encode()).hexdigest()[:24]
    for table, name, kind, definition, *_rest in snapshot["constraints"]:
        if name in names:
            continue
        if name in {
            "department_membership_lifecycle_provenance",
            "station_assignment_lifecycle_provenance",
        }:
            # These two equivalent expressions have different AND ordering in
            # historical SQL and model state. The catalog test checks all values.
            dump = dump.replace(definition, "CHECK (<validated_lifecycle_provenance>)")
        else:
            names[name] = (
                "con_" + hashlib.sha256((table + kind + definition).encode()).hexdigest()[:24]
            )
    for name in sorted(names, key=len, reverse=True):
        dump = re.sub(
            r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", names[name], dump
        )
    dump = re.sub(r"(?m)^\\(?:un)?restrict .*\n?", "", dump)
    statements = []
    for statement in sqlparse.split(sqlparse.format(dump, strip_comments=True)):
        statement = statement.strip()
        if statement.startswith("CREATE TABLE "):
            head, body = statement.split("(\n", 1)
            columns, tail = body.rsplit("\n)", 1)
            statement = (
                head
                + "(\n"
                + "\n".join(sorted(line.strip().rstrip(",") for line in columns.splitlines()))
                + "\n)"
                + tail
            )
        statements.append(statement)
    return sorted(statements)


@contextmanager
def disposable_database():
    """Use only the configured test connection and its empty PostGIS template."""
    from django.conf import settings

    if settings.SETTINGS_MODULE != "config.settings.test":
        raise RuntimeError("Disposable migration validation requires config.settings.test")
    original = connections["default"]
    config = deepcopy(original.settings_dict)
    name = "test_baseline_" + uuid4().hex
    config.update(NAME=name, CONN_MAX_AGE=0)
    with original.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(config["TEST"]["TEMPLATE"])
            )
        )
    disposable = original.__class__(config, alias="default")
    connections["default"] = disposable
    try:
        yield disposable
    finally:
        disposable.close()
        connections["default"] = original
        assert name.startswith("test_baseline_") and len(name) == 46
        with original.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class HistoricalMigrationLoader(MigrationLoader):
    """Keep replacement files out of tests that explicitly target original nodes."""

    def load_disk(self):
        super().load_disk()
        self.disk_migrations = {
            key: migration
            for key, migration in self.disk_migrations.items()
            if not migration.replaces
        }


def historical_executor(connection):
    executor = MigrationExecutor(connection)
    executor.loader = HistoricalMigrationLoader(connection, replace_migrations=False)
    return executor


def catalog(connection):
    """Capture application schema definitions; never read application data."""
    queries = {
        "columns": """
            SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod),
                   a.attnotnull, a.attidentity, pg_get_expr(d.adbin,d.adrelid)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname<>'django_migrations'
            ORDER BY c.relname, a.attname
        """,
        "constraints": """
            SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid),
                   con.condeferrable, con.condeferred, con.convalidated
            FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname<>'django_migrations'
            ORDER BY 1,2,3
        """,
        "indexes": """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname='public' AND tablename<>'django_migrations' ORDER BY 1,2
        """,
        "functions": """
            SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid),
                   pg_get_userbyid(p.proowner), p.proacl::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND (p.proname LIKE 'publications_%'
                OR p.proname LIKE 'file_library_%' OR p.proname='reject_audit_event_mutation')
            ORDER BY 1,2
        """,
        "triggers": """
            SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY 1,2
        """,
        "ownership": """
            SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relacl::text
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','S')
                AND c.relname<>'django_migrations' AND c.relname<>'django_migrations_id_seq'
            ORDER BY 1
        """,
    }
    result = {}
    with connection.cursor() as cursor:
        for key, query in queries.items():
            cursor.execute(query)
            result[key] = cursor.fetchall()
    return result
