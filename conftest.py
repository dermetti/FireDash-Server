"""Project-wide pytest safety hooks."""

import os
import shutil
import stat
from pathlib import Path

import pytest

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
