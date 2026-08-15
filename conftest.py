"""Project-wide pytest safety hooks."""

import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
TEST_TEMP_PREFIX = ".test-tmp"


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove an explicitly supplied repository-local pytest base directory.

    The external Windows test harness sometimes invokes pytest with
    ``--basetemp=.test-tmp<N>``. Pytest intentionally retains explicit bases,
    so restrict cleanup to that exact, generated location rather than letting
    test artifacts accumulate in the working tree.
    """

    configured_base = session.config.getoption("basetemp")
    if not configured_base:
        return

    base_path = Path(configured_base).resolve()
    if base_path.parent == PROJECT_ROOT and base_path.name.startswith(TEST_TEMP_PREFIX):
        shutil.rmtree(base_path, ignore_errors=True)
