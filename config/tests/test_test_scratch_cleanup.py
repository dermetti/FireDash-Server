import os
import stat

import pytest

from conftest import PROJECT_ROOT, remove_repository_test_scratch


def test_root_test_scratch_cleanup_removes_read_only_children():
    scratch = PROJECT_ROOT / ".pytest-cache-cleanup-unit"
    scratch.mkdir(parents=True, exist_ok=True)
    child = scratch / "credential-fixture"
    child.write_text("test-only", encoding="utf-8")
    os.chmod(child, stat.S_IRUSR)

    remove_repository_test_scratch(scratch)

    assert not scratch.exists()


def test_root_test_scratch_cleanup_refuses_non_generated_path():
    with pytest.raises(ValueError, match="non-test repository path"):
        remove_repository_test_scratch(PROJECT_ROOT / "not-test-scratch")
