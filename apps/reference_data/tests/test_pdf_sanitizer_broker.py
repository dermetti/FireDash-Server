import importlib.util
import os
import stat
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _output(broker: ModuleType) -> str:
    return os.path.join(broker.SANITIZER_OUTPUT_ROOT, JOB, broker.OUTPUT_NAME)


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
    output = _output(broker)
    fd = 7
    with (
        patch.object(broker.subprocess, "run", return_value=result) as run,
        patch.object(broker.os, "open", return_value=fd) as opened,
        patch.object(
            broker.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=10)
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
    opened.assert_called_once_with(output, os.O_RDONLY | broker._O_NOFOLLOW)
    fchown.assert_called_once_with(fd, 1001, 1001)
    fchmod.assert_called_once_with(fd, 0o600)
    close.assert_called_once_with(fd)


def test_run_job_handles_sanitizer_failure(broker: ModuleType) -> None:
    result = MagicMock(returncode=1)
    with patch.object(broker.subprocess, "run", return_value=result):
        assert broker.run_job(JOB) == "ERR failed"


def test_run_job_handles_timeout(broker: ModuleType) -> None:
    with patch.object(
        broker.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["/bin/systemctl"], 75),
    ):
        assert broker.run_job(JOB) == "ERR timeout"


def test_run_job_handles_oserror(broker: ModuleType) -> None:
    with patch.object(broker.subprocess, "run", side_effect=OSError()):
        assert broker.run_job(JOB) == "ERR failed"


def test_run_job_rejects_missing_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", side_effect=OSError()),
    ):
        assert broker.run_job(JOB) == "ERR failed"


def test_run_job_rejects_non_regular_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", return_value=fd),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFLNK, st_size=10),
        ),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR failed"
    close.assert_called_once_with(fd)


def test_run_job_rejects_oversized_output(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", return_value=fd),
        patch.object(
            broker.os,
            "fstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=broker.MAX_OUTPUT_BYTES + 1),
        ),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR failed"
    close.assert_called_once_with(fd)


def test_run_job_handles_finalization_failure(broker: ModuleType) -> None:
    result = MagicMock(returncode=0)
    fd = 5
    with (
        patch.object(broker.subprocess, "run", return_value=result),
        patch.object(broker.os, "open", return_value=fd),
        patch.object(
            broker.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=10)
        ),
        patch.object(broker, "_owner_ids", return_value=(1001, 1001)),
        patch.object(broker.os, "fchown", side_effect=OSError(), create=True),
        patch.object(broker.os, "close") as close,
    ):
        assert broker.run_job(JOB) == "ERR failed"
    close.assert_called_once_with(fd)


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
