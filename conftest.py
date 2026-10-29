"""Project-wide pytest safety hooks."""

import copy
import os
import shutil
import stat
import uuid
from pathlib import Path

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parent
TEST_TEMP_PREFIXES = (".test-tmp", ".pytest-cache-", ".pytest-ws4")


def _is_repository_test_scratch(path: Path) -> bool:
    """Return whether ``path`` is one generated pytest scratch directory at root."""
    return path.parent == PROJECT_ROOT and path.name.startswith(TEST_TEMP_PREFIXES)


def _restore_cleanup_access(function, path, _exc_info) -> None:
    """Let pytest remove a test-created read-only child during teardown.

    Permission assertions still run before this callback.  It is deliberately
    limited to a known test scratch tree, and only restores owner write/traverse
    access long enough for ``shutil.rmtree`` to finish.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(path)
    except OSError:
        # The normal test process may not be the ACL owner of an abandoned tree.
        # Leave it for an operator rather than widening any unrelated ACL.
        pass


def remove_repository_test_scratch(path: Path) -> None:
    """Remove only a known generated root scratch directory, if present."""
    resolved = path.resolve()
    if not _is_repository_test_scratch(resolved):
        raise ValueError("Refusing to remove a non-test repository path.")
    shutil.rmtree(resolved, onerror=_restore_cleanup_access, ignore_errors=False)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Sweep abandoned explicit root bases left by an interrupted prior run."""
    for candidate in PROJECT_ROOT.iterdir():
        if candidate.is_dir() and _is_repository_test_scratch(candidate.resolve()):
            remove_repository_test_scratch(candidate)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove an explicitly supplied repository-local pytest base directory.

    The external Windows test harness sometimes invokes pytest with
    ``--basetemp=.test-tmp<N>`` (and earlier ``.pytest-*`` variants). Pytest
    intentionally retains explicit bases, so restrict cleanup to generated
    root paths rather than letting artifacts accumulate in the working tree.
    """

    configured_base = session.config.getoption("basetemp")
    if not configured_base:
        return

    base_path = Path(configured_base).resolve()
    if _is_repository_test_scratch(base_path):
        remove_repository_test_scratch(base_path)


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
        target = before if isinstance(before, tuple) else ("publications", before)
        graph = executor.loader.graph
        allowed = set(graph.forwards_plan(target))
        roots = [
            leaf
            for leaf in graph.leaf_nodes()
            if leaf[0] != target[0]
            and all(node[0] != target[0] or node in allowed for node in graph.forwards_plan(leaf))
        ]
        executor.migrate([*roots, target])
        yield disposable
    finally:
        disposable.close()
        connections["default"] = original
        assert name.startswith("test_phase2_") and len(name) == 44
        with original.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
