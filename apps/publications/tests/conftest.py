"""Disposable databases for forward-only publication migration tests."""

import copy
import uuid

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql


@pytest.fixture
def disposable_migration_database(transactional_db, request):
    """Start from an empty local test template without reversing destructive migrations."""
    original = connections["default"]
    assert original.vendor == "postgresql"
    name = "test_phase2_" + uuid.uuid4().hex
    config = copy.deepcopy(original.settings_dict)
    config["NAME"] = name
    config["CONN_MAX_AGE"] = 0
    with original.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(config["TEST"]["TEMPLATE"])
            )
        )
    disposable = original.__class__(config, alias="default")
    connections["default"] = disposable
    try:
        # Historical upgrade tests must start before the schema under test,
        # without applying dependent data migrations or reversing destructive DDL.
        executor = MigrationExecutor(disposable)
        before = getattr(request, "param", "0031_unified_document_generation")
        target = ("publications", before)
        graph = executor.loader.graph
        allowed = set(graph.forwards_plan(target))
        roots = [
            leaf
            for leaf in graph.leaf_nodes()
            if leaf[0] != "publications"
            and all(
                node[0] != "publications" or node in allowed for node in graph.forwards_plan(leaf)
            )
        ]
        executor.migrate([*roots, target])
        yield disposable
    finally:
        disposable.close()
        connections["default"] = original
        assert name.startswith("test_phase2_") and len(name) == 44
        with original.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
