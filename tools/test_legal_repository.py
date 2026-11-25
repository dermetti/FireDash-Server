"""Public legal input integrity and signing-material packaging boundaries."""

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_INPUTS = (
    "legal/EULA.public.de.md",
    "legal/EULA.public.en.md",
    "legal/provider-manifest.json",
    "legal/provider-signing.pub",
)


def git(*args, input=None):
    return subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args],
        cwd=ROOT,
        input=input,
        text=True,
        capture_output=True,
        check=False,
    )


def test_approved_templates_match_manifest_and_use_lf():
    manifest = json.loads((ROOT / "legal/provider-manifest.json").read_bytes())
    assert manifest["agreement_version"] == "1.0-beta"
    assert manifest["agreement_revision"] == 3
    assert manifest["provider_revision"] == 1
    assert manifest["authoritative_language"] == "de"
    assert manifest["schema_version"] == 1
    assert manifest["effective_date"] == "2026-11-25"
    assert manifest["status"] == "approved"
    expected = {
        "de": "f8dc87445df16481437f7c85541b527f0bf987a227d7b5b2ad01f9c66065a4e3",
        "en": "ec256509c211acaf6b8ff82cd30250db1e8f94eba9ef0bbafeb8b3b20242b581",
    }
    for language in ("de", "en"):
        record = manifest["languages"][language]
        content = (ROOT / "legal" / record["template"]).read_bytes()
        assert (
            hashlib.sha256(content).hexdigest() == record["template_sha256"] == expected[language]
        )
    for name in PUBLIC_INPUTS:
        assert b"\r" not in (ROOT / name).read_bytes()


def test_public_inputs_are_not_ignored_and_checkout_with_lf():
    result = git(
        "check-ignore", "--no-index", "-z", "--stdin", input="\0".join(PUBLIC_INPUTS) + "\0"
    )
    assert result.returncode == 1, result.stderr or result.stdout
    result = git("check-attr", "text", "eol", "--", *PUBLIC_INPUTS)
    assert result.returncode == 0, result.stderr
    for name in PUBLIC_INPUTS:
        assert f"{name}: text: set" in result.stdout
        assert f"{name}: eol: lf" in result.stdout


def test_revision_3_documentation_and_reference_identity():
    public = (ROOT / "legal/README.md").read_text(encoding="utf-8")
    assert "| Agreement revision | 3 |" in public
    assert "third-party hosting" in public and "§ 16a" in public
    assert "does not automatically bind another legally independent organization" in public
    maintenance = (ROOT / "deploy/EULA-maintenance.md").read_text(encoding="utf-8")
    current, historical = maintenance.split("## Historical Revision 2 references", 1)
    assert "## Approved Revision 3 comparison" in current
    assert '"1.0-beta", 3, 1' in current
    hashes = {
        "de": "d2ba7f36392eccf63e7b6b27b91ff845cf654575c9d70623c40fe7ec6af6409e",
        "en": "828153dfad9bf33618d7830bf6e27d1ddce37cd7c05c1a5fb4af74c82cdb17ef",
    }
    identity = {
        "schema_version": 1,
        "agreement_version": "1.0-beta",
        "agreement_revision": 3,
        "provider_revision": 1,
        "assembled_sha256": hashes,
    }
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    expected_set = "34fbea5a0b478d05fbf24b857ea8ad2c787b79621ace24c2470ef6909755528e"
    assert hashlib.sha256(canonical).hexdigest() == expected_set
    for value in (*hashes.values(), expected_set):
        assert value in current
    for value in (
        "15188bd7ab716abccfccb5981f8066a966a7de82d4746464f5ef1274279e1aa1",
        "7de85b1cd3c45716e62e0f8108c269b95c84ed102997d5bf3a292e229116f1d9",
        "38bf58bd49fd8f70fcb64d46f7d7a77c548ef9b27853b0cbee91ee549cb61b56",
    ):
        assert value in historical
    procedure = current.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    compile(procedure, "maintainer-reference-procedure", "exec")


def test_synthetic_private_paths_ignored_and_excluded_from_git_archives():
    # Virtual path queries only: never inspect the real .secrets directory.
    paths = (
        ".secrets/provider-signing.key",
        ".secrets/synthetic-backup.txt",
        "provider-signing.key",
        "nested/provider-signing.key",
        "nested/deeper/provider-signing.key",
    )
    result = git("check-ignore", "--no-index", "-z", "--stdin", input="\0".join(paths) + "\0")
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.rstrip("\0").split("\0")) == set(paths)
    archive_paths = (".secrets", "nested/.secrets", *paths[2:])
    result = git("check-attr", "export-ignore", "--", *archive_paths)
    assert result.returncode == 0, result.stderr
    for name in archive_paths:
        assert f"{name}: export-ignore: set" in result.stdout
    result = git("ls-files", "-z")
    assert result.returncode == 0, result.stderr
    for name in result.stdout.split("\0"):
        parts = name.split("/")
        assert ".secrets" not in parts
        assert parts[-1] != "provider-signing.key"


def test_release_and_ci_tar_exclude_synthetic_signing_material(tmp_path):
    tar = shutil.which("tar")
    if not tar:
        pytest.skip("tar executable unavailable")
    release = (ROOT / "deploy/lib/release.sh").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    commands = re.findall(r"\| tar (.*?) -x -C", release + "\n" + workflow)
    assert len(commands) == 3  # Release and both container source snapshots.
    assert "$PWD:/source:ro" not in workflow
    assert workflow.count("$source_snapshot:/source:ro") == 2
    names = (
        ".secrets/provider-signing.key",
        ".secrets/synthetic-backup.txt",
        "provider-signing.key",
        "nested/provider-signing.key",
        "nested/.secrets/synthetic-backup.txt",
        *PUBLIC_INPUTS,
        "LICENSE",
    )
    archive = tmp_path / "synthetic.tar"
    with tarfile.open(archive, "w") as stream:
        for name in names:
            content = b"synthetic packaging fixture, not signing material\n"
            info = tarfile.TarInfo(name)
            info.size = len(content)
            stream.addfile(info, io.BytesIO(content))
    for index, flags in enumerate(commands):
        destination = tmp_path / str(index)
        destination.mkdir()
        # These are the actual extraction options in each production mechanism.
        result = subprocess.run(
            [
                tar,
                *shlex.split(flags),
                "-xf",
                str(archive),
                "-C",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        included = {
            p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
        }
        assert included == {*PUBLIC_INPUTS, "LICENSE"}


def test_artifact_exclusions_are_explicit():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "!dependency-audit/**/.secrets/**" in workflow
    assert "!dependency-audit/**/provider-signing.key" in workflow


def test_ci_snapshot_permissions_preserve_filtering_and_isolation():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    snapshots = re.findall(
        r"source_snapshot=\$\(mktemp -d\)(.*?)docker run ([^\n]+)", workflow, re.DOTALL
    )
    assert len(snapshots) == 2
    for preparation, container in snapshots:
        assert "trap 'rm -rf -- \"$source_snapshot\"' EXIT" in preparation
        extraction = (
            "git archive HEAD | tar --exclude='.secrets' --exclude='provider-signing.key' "
            '-x -C "$source_snapshot"'
        )
        permissions = 'chmod -R a+rX "$source_snapshot"'
        assert preparation.count(extraction) == 1
        assert preparation.count(permissions) == 1
        assert preparation.index(extraction) < preparation.index(permissions)
        assert '-v "$source_snapshot:/source:ro"' in container
        assert "$PWD" not in container
    assert "runuser -u pdf_worker_test -- /usr/bin/python3 -I " in workflow


@pytest.mark.skipif(os.name != "posix", reason="Requires native Unix permission semantics")
def test_ci_snapshot_permissions_with_restrictive_synthetic_fixture(tmp_path):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    permissions = re.findall(r'^\s*(chmod -R a\+rX "\$source_snapshot")$', workflow, re.MULTILINE)
    assert len(permissions) == 2 and permissions[0] == permissions[1]
    snapshot = tmp_path / "synthetic-snapshot"
    snapshot.mkdir(mode=0o700)
    nested = snapshot / "deploy/tests"
    nested.mkdir(parents=True)
    nested.parent.chmod(0o700)
    nested.chmod(0o700)
    worker = nested / "native_pdf_worker.py"
    worker.write_bytes(b"# Synthetic public worker fixture\n")
    worker.chmod(0o600)
    result = subprocess.run(
        ["sh", "-eu", "-c", "source_snapshot=$1\n" + permissions[0], "fixture", str(snapshot)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for directory in (snapshot, nested.parent, nested):
        assert directory.stat().st_mode & 0o777 == 0o755
    assert worker.stat().st_mode & 0o777 == 0o644
    assert worker.read_bytes() == b"# Synthetic public worker fixture\n"
