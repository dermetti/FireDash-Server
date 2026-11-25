"""Public legal input integrity and signing-material packaging boundaries."""

import hashlib
import io
import json
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
    assert manifest["agreement_revision"] == 2
    assert manifest["provider_revision"] == 1
    assert manifest["authoritative_language"] == "de"
    for language in ("de", "en"):
        record = manifest["languages"][language]
        content = (ROOT / "legal" / record["template"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == record["template_sha256"]
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
