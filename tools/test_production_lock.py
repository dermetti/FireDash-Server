"""Offline lock/input consistency and cross-target dependency-graph regressions."""

import pytest

from tools import production_lock as lock


def item(name, version="1", dependencies=()):
    return {"metadata": {"name": name, "version": version, "requires_dist": dependencies}}


def test_reviewed_production_lock_matches_direct_input_and_constraints():
    lock.check()


def test_linux_closure_uses_target_markers_and_required_extras():
    items = {
        "root": item(
            "root",
            dependencies=[
                'linux; sys_platform == "linux"',
                'windows; sys_platform == "win32"',
                'binary; extra == "binary"',
            ],
        ),
        "linux": item("linux"),
        "windows": item("windows"),
        "binary": item("binary"),
    }
    assert set(lock.dependency_closure(items, [lock.Requirement("root[binary]==1")])) == {
        "root",
        "linux",
        "binary",
    }


@pytest.mark.parametrize("dependencies", [["missing"], ["root==2"]])
def test_cross_target_missing_or_conflicting_dependencies_fail(dependencies):
    with pytest.raises(ValueError):
        lock.dependency_closure(
            {"root": item("root", dependencies=dependencies)}, [lock.Requirement("root==1")]
        )


@pytest.mark.parametrize("change", ["hash", "version", "constraint", "duplicate"])
def test_lock_drift_rejected(tmp_path, monkeypatch, change):
    content = lock.LOCK.read_text()
    constraints = lock.CONSTRAINTS.read_text()
    if change == "hash":
        content = content.replace("--hash=sha256:", "--hash=sha256:invalid", 1)
    elif change == "version":
        content = content.replace("django==5.2.17", "django==1.0")
        constraints = constraints.replace("django==5.2.17", "django==1.0")
    elif change == "constraint":
        constraints += "unreviewed==1\n"
    else:
        content += next(line for line in content.splitlines() if "--hash" in line) + "\n"
    path = tmp_path / "production.lock"
    path.write_text(content)
    pins = tmp_path / "constraints.txt"
    pins.write_text(constraints)
    monkeypatch.setattr(lock, "LOCK", path)
    monkeypatch.setattr(lock, "CONSTRAINTS", pins)
    with pytest.raises(ValueError):
        lock.check()
