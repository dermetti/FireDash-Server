"""Actual Linux DAC regression; no service accounts or host directories are changed."""

import json
import os
import shutil
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest


def _check_native_permissions(scratch: Path) -> None:
    import grp
    import pwd

    from django.conf import settings
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import override_settings

    from apps.reference_data import pdf_sandbox, storage

    if not settings.configured:
        settings.configure()

    # Numeric process identities are disposable: do not create or modify host accounts.
    used = {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}
    backend, sanitizer, unrelated = [uid for uid in range(60000, 61000) if uid not in used][:3]
    scratch.chmod(0o755)
    # Relative paths let children exercise DAC without traversing pytest's private ancestors.
    roots = {name: Path(name) for name in ("quarantine", "output", "accepted")}
    for root in roots.values():
        (scratch / root).mkdir()
        os.chown(scratch / root, backend, sanitizer)
        (scratch / root).chmod(0o2710)
    broker = Path("broker.sock")
    (scratch / broker).touch()
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    output = roots["output"] / job_id / "sanitized.pdf"
    policy = output.with_name("request.json")

    def as_identity(uid, action):
        pid = os.fork()
        if pid == 0:
            try:
                # Start here to avoid changing pytest scratch ancestors' permissions.
                os.chdir(scratch)
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                os.umask(0o077)
                action()
            except BaseException:
                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, f"Permission checks failed for UID {uid}"

    def create_policy():
        storage.write_quarantine(SimpleUploadedFile("synthetic.pdf", b"%PDF-1.4 fixture"))
        storage.output_path(job_id=job_id)
        # Only transport is replaced. File creation, chmod, inheritance and access are native.
        with patch.object(pdf_sandbox, "_request_sanitization", return_value="ERR output"):
            with pytest.raises(pdf_sandbox.PdfSanitizerError, match="rejected"):
                pdf_sandbox.sanitize(
                    quarantined_input=roots["quarantine"] / job_id / "input.pdf",
                    sanitized_output=output,
                )
        assert policy.stat().st_gid == backend  # The job chmod clears inherited setgid.
        assert stat.S_IMODE(policy.stat().st_mode) == 0o644

    def read_policy():
        assert json.loads(policy.read_text()) == {
            "strict": False,
            "max_input_bytes": 1024,
            "max_output_bytes": 1024,
            "max_pages": 10,
        }
        with pytest.raises(PermissionError):
            policy.open("w")
        with pytest.raises(PermissionError):
            policy.chmod(0o666)
        # Match the worker's UMask=0077; changing policy mode must not affect outputs.
        output.write_bytes(b"%PDF-1.4 synthetic output")
        output.with_name("result.txt").write_text("synthetic result\n")

    def deny_unrelated():
        for path in (policy.parent, roots["quarantine"]):
            with pytest.raises(PermissionError):
                os.chdir(path)
        for path in (policy, output, output.with_name("result.txt")):
            with pytest.raises(PermissionError):
                path.read_bytes()

    with override_settings(
        REFERENCE_DATA_QUARANTINE_ROOT=roots["quarantine"],
        REFERENCE_DATA_SANITIZER_OUTPUT_ROOT=roots["output"],
        REFERENCE_DATA_ACCEPTED_ROOT=roots["accepted"],
        PDF_SANITIZER_BROKER_SOCKET=str(broker),
        PDF_SANITIZER_TIMEOUT_SECONDS=1,
        MAX_PDF_INPUT_BYTES=1024,
        MAX_PDF_OUTPUT_BYTES=1024,
        MAX_PDF_PAGES=10,
    ):
        as_identity(backend, create_policy)
        as_identity(sanitizer, read_policy)
        as_identity(unrelated, deny_unrelated)

    assert stat.S_IMODE((scratch / output.parent).stat().st_mode) == 0o730
    assert (scratch / output.parent).stat().st_gid == sanitizer
    for root in roots.values():
        assert stat.S_IMODE((scratch / root).stat().st_mode) == 0o2710
    for path in (scratch / roots["quarantine"]).iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o2700
        assert stat.S_IMODE((path / "input.pdf").stat().st_mode) == 0o640
    for path in (output, output.with_name("result.txt")):
        assert stat.S_IMODE((scratch / path).stat().st_mode) == 0o600
        assert (scratch / path).stat().st_uid == sanitizer


def _run_native_permissions(scratch: Path) -> None:
    try:
        _check_native_permissions(scratch)
    finally:
        # Remove identity-owned fixtures while still privileged, including on assertion failure.
        for name in ("quarantine", "output", "accepted"):
            path = scratch / name
            if path.exists():
                shutil.rmtree(path)
        (scratch / "broker.sock").unlink(missing_ok=True)
        scratch.chmod(0o700)


@pytest.mark.skipif(sys.platform != "linux", reason="Requires native Linux DAC and identities")
def test_native_pdf_policy_permissions(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        _run_native_permissions(tmp_path)
        return
    sudo = shutil.which("sudo")
    if sudo is None:
        pytest.skip("Requires root or non-interactive sudo to use disposable process identities")
    probe = subprocess.run(
        [sudo, "-n", sys.executable, "-c", "import os; assert os.geteuid() == 0"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        pytest.skip("Native identity-switch privileges unavailable (non-interactive sudo denied)")
    result = subprocess.run(
        [
            sudo,
            "-n",
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_run_native_permissions'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
