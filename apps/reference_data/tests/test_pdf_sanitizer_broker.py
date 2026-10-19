import importlib.util
import os
import signal
import stat
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

BROKER_PATH = (
    Path(__file__).resolve().parents[3] / "deploy" / "scripts" / "fire-pdf-sanitizer-broker"
)

JOB = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture(scope="module")
def broker() -> ModuleType:
    loader = SourceFileLoader("fire_pdf_sanitizer_broker", str(BROKER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_job_id_accepts_only_canonical_lowercase_uuid(broker: ModuleType) -> None:
    assert broker.validate_job_id(JOB) == JOB
    assert broker.validate_job_id(JOB.upper()) is None
    assert broker.validate_job_id("../../etc/passwd") is None
    assert broker.validate_job_id("foo; rm -rf /") is None
    assert broker.validate_job_id("fire-pdf-sanitizer@foo.service") is None
    assert broker.validate_job_id("") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"../../etc/passwd\n",
        b"foo; rm -rf /\n",
        b"fire-pdf-sanitizer@foo.service\n",
        JOB.upper().encode("ascii") + b"\n",
        JOB.encode("ascii") + b" extra\n",
        JOB.encode("ascii") + b"\n\n",
        JOB.encode("ascii"),
        b"\xff\xfe\n",
        b"a" * 65 + b"\n",
    ],
)
def test_parse_request_rejects_malformed_input(broker: ModuleType, raw: bytes) -> None:
    with patch.object(broker, "run_job", return_value="OK") as run_job:
        assert broker.parse_request(raw) == "ERR invalid"
    run_job.assert_not_called()


def test_parse_request_accepts_valid_uuid_and_delegates(broker: ModuleType) -> None:
    with patch.object(broker, "run_job", return_value="OK") as run_job:
        assert broker.parse_request(JOB.encode("ascii") + b"\n") == "OK"
    run_job.assert_called_once_with(JOB)


def test_run_job_invokes_only_fixed_sanitizer_template(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 7
    with (
        patch.object(broker.subprocess, "run", return_value=result) as run,
        patch.object(broker.os, "open", side_effect=[3, 4, fd]) as opened,
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=10, st_nlink=1),
        ),
        patch.object(broker, "_owner_ids", return_value=(1001, 1001)),
        patch.object(broker.os, "fchown", create=True) as fchown,
        patch.object(broker.os, "fchmod", create=True) as fchmod,
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "OK"

    run.assert_called_once_with(
        ["/bin/systemctl", "start", "--wait", f"fire-pdf-sanitizer@{JOB}.service"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=broker.SANITIZER_TIMEOUT_SECONDS,
    )
    assert opened.call_args_list == [
        call(broker.SANITIZER_OUTPUT_ROOT, os.O_RDONLY | broker._O_DIRECTORY | broker._O_NOFOLLOW),
        call(JOB, os.O_RDONLY | broker._O_DIRECTORY | broker._O_NOFOLLOW, dir_fd=3),
        call(broker.OUTPUT_NAME, os.O_RDONLY | broker._O_NONBLOCK | broker._O_NOFOLLOW, dir_fd=4),
    ]
    fchown.assert_called_once_with(fd, 1001, 1001)
    fchmod.assert_called_once_with(fd, 0o600)
    assert close.call_args_list == [call(fd), call(4), call(3)]


@pytest.mark.parametrize(
    "returncode",
    [1, 2, 3, 203],
)
def test_run_job_treats_every_nonzero_as_infrastructure_fatal(
    broker: ModuleType, returncode: int
) -> None:
    # qpdf uses exit status 2 for both document-content rejections and for
    # operational failures (I/O, permissions, filesystem, bad invocation), so the
    # broker must not interpret any non-zero ``start --wait`` result as a
    # skippable content rejection. Every non-zero result is infrastructure-fatal.
    result = MagicMock(returncode=returncode)
    with patch.object(broker.subprocess, "run", return_value=result):
        assert broker.run_job(JOB) == "ERR output"


def test_run_job_has_no_exit_status_content_classification(broker: ModuleType) -> None:
    # There must be no code path that maps a qpdf exit status to a content
    # rejection token; content vs infrastructure is intentionally not inferable
    # from the unit exit status.
    assert not hasattr(broker, "_classify_unit_failure")
    assert not hasattr(broker, "_show_unit_state")


def test_run_job_handles_timeout(broker: ModuleType) -> None:
    with patch.object(
        broker.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["/bin/systemctl"], 75),
    ):
        assert broker.run_job(JOB) == "ERR timeout"


def test_run_job_handles_oserror(broker: ModuleType) -> None:
    with patch.object(broker.subprocess, "run", side_effect=OSError()):
        assert broker.run_job(JOB) == "ERR output"


def test_run_job_rejects_missing_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", side_effect=OSError()),
    ):
        assert broker.run_job(JOB) == "ERR output"


def test_run_job_rejects_non_regular_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", side_effect=[3, 4, fd]),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFLNK, st_size=10),
        ),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR output"
    assert close.call_args_list == [call(fd), call(4), call(3)]


def test_run_job_rejects_oversized_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", side_effect=[3, 4, fd]),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(
                st_mode=stat.S_IFREG, st_size=broker.MAX_OUTPUT_BYTES + 1, st_nlink=1
            ),
        ),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR output"
    assert close.call_args_list == [call(fd), call(4), call(3)]


def test_run_job_handles_finalization_failure(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", side_effect=[3, 4, fd]),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=10, st_nlink=1),
        ),
        patch.object(broker, "_owner_ids", return_value=(1001, 1001)),
        patch.object(broker.os, "fchown", side_effect=OSError(), create=True),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR output"
    assert close.call_args_list == [call(fd), call(4), call(3)]


@pytest.mark.parametrize("size,links", [(0, 1), (10, 2)])
def test_run_job_rejects_empty_or_hard_linked_output(broker: ModuleType, size, links) -> None:
    with (
        patch.object(broker.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
        patch.object(broker.os, "open", side_effect=[3, 4, 5]),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=size, st_nlink=links),
        ),
        patch.object(broker.os, "fchown", create=True) as chown,
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR output"
    chown.assert_not_called()
    assert close.call_args_list == [call(5), call(4), call(3)]


@pytest.mark.parametrize("failed_open", [0, 1, 2])
def test_run_job_closes_directory_descriptors_on_open_failure(broker: ModuleType, failed_open):
    with (
        patch.object(broker.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
        patch.object(broker.os, "open", side_effect=[*range(3, 3 + failed_open), OSError()]),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR output"
    assert close.call_args_list == [call(fd) for fd in reversed(range(3, 3 + failed_open))]


@pytest.fixture
def native_output(broker, tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("requires Linux dir_fd, O_NOFOLLOW, ownership and FIFO semantics")
    root = tmp_path / "output"
    job = root / JOB
    job.mkdir(parents=True)
    output = job / broker.OUTPUT_NAME
    output.write_bytes(b"%PDF-1.4 fixture")
    monkeypatch.setattr(broker, "SANITIZER_OUTPUT_ROOT", str(root))
    monkeypatch.setattr(broker, "_owner_ids", lambda: (os.getuid(), os.getgid()))
    # Only job execution is mocked; descriptor/path validation uses the real kernel.
    monkeypatch.setattr(broker.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    return root, job, output


@pytest.mark.parametrize("attack", ["root-link", "job-link", "file-link", "hard-link", "fifo"])
def test_native_output_rejects_path_redirection_and_special_files(
    broker, native_output, tmp_path, attack
):
    root, job, output = native_output
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / broker.OUTPUT_NAME
    victim.write_bytes(b"untouched")
    os.chmod(victim, 0o640)
    before = victim.stat()
    if attack == "root-link":
        root.rename(tmp_path / "original-root")
        (outside / JOB).mkdir()
        (outside / JOB / broker.OUTPUT_NAME).write_bytes(b"outside job")
        root.symlink_to(outside, target_is_directory=True)
    elif attack == "job-link":
        job.rename(root / "original-job")
        job.symlink_to(outside, target_is_directory=True)
    else:
        output.unlink()
        if attack == "file-link":
            output.symlink_to(victim)
        elif attack == "hard-link":
            os.link(victim, output)
        else:
            os.mkfifo(output)
    if attack == "fifo":
        previous_handler = signal.signal(
            signal.SIGALRM, lambda *_: pytest.fail("broker blocked opening a FIFO")
        )
        signal.alarm(3)
    try:
        with patch.object(broker.os, "fchown", wraps=os.fchown) as chown:
            assert broker.run_job(JOB) == "ERR output"
    finally:
        if attack == "fifo":
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    chown.assert_not_called()
    assert victim.read_bytes() == b"untouched"
    after = victim.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        before.st_uid,
        before.st_gid,
        before.st_mode,
    )


def test_native_directory_replacement_cannot_redirect_finalization(
    broker, native_output, tmp_path, monkeypatch
):
    root, job, output = native_output
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / broker.OUTPUT_NAME
    victim.write_bytes(b"untouched")
    os.chmod(victim, 0o640)
    original_open = os.open

    def replace_after_directory_open(path, flags, **kwargs):
        if path == broker.OUTPUT_NAME:
            job.rename(root / "pinned-job")
            job.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(broker.os, "open", replace_after_directory_open)
    assert broker.run_job(JOB) == "OK"
    assert stat.S_IMODE((root / "pinned-job" / output.name).stat().st_mode) == 0o600
    assert victim.read_bytes() == b"untouched"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o640


def test_broker_unit_files_use_accept_yes_and_inetd_stdio() -> None:
    root = Path(__file__).resolve().parents[3]
    socket_unit = (root / "deploy" / "systemd" / "fire-pdf-sanitizer-broker.socket").read_text()
    service_unit = (root / "deploy" / "systemd" / "fire-pdf-sanitizer-broker@.service").read_text()

    assert "Accept=yes" in socket_unit
    assert "SocketGroup=fire_backend" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "StandardInput=socket" in service_unit
    assert "StandardOutput=inherit" in service_unit
    assert "StandardError=journal" in service_unit
    assert "NoNewPrivileges=true" in service_unit
    assert "EnvironmentFile=" not in service_unit
    assert "LoadCredential=" not in service_unit


def test_sanitizer_unit_tolerates_qpdf_warnings_only_exit() -> None:
    root = Path(__file__).resolve().parents[3]
    sanitizer_unit = (root / "deploy" / "systemd" / "fire-pdf-sanitizer@.service").read_text()

    # qpdf exits 3 on warnings-without-errors; --warning-exit-0 maps that to 0 so
    # the oneshot unit does not fail on recoverable structural warnings while a
    # real qpdf error (status 2) still fails the unit.
    assert "--warning-exit-0" in sanitizer_unit
    assert "ExecStart=/usr/bin/qpdf" in sanitizer_unit
    assert "--deterministic-id" in sanitizer_unit
