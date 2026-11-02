"""Installer runtime probe coverage without entering host namespaces or services."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def helper():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "deploy/scripts/report-transfer-credential.py")
    )["main"]


def test_probe_uses_running_process_environment_without_secret_arguments(helper, monkeypatch):
    environment = {
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "CREDENTIALS_DIRECTORY": "/run/credentials/fire-backend.service",
        "POSTGRES_PASSWORD": "synthetic-database-password",
    }
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _: b"\0".join(f"{k}={v}".encode() for k, v in environment.items()),
    )
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["helper", "probe", "123", "/srv/firedash/current", "v2", "https://fire.test"]
    )
    helper()
    args, kwargs = run.call_args
    assert args[0][:9] == [
        "nsenter",
        "--target",
        "123",
        "--mount",
        "--",
        "runuser",
        "-u",
        "fire_backend",
        "--",
    ]
    assert args[0][-3:] == ["runtime", "v2", "https://fire.test"]
    assert environment["POSTGRES_PASSWORD"] not in str(args)
    assert kwargs["env"]["POSTGRES_PASSWORD"] == environment["POSTGRES_PASSWORD"]
    assert kwargs["check"] and kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    # Failed namespace entry / application validation cannot look successful.
    run.side_effect = subprocess.CalledProcessError(1, "nsenter")
    with pytest.raises(subprocess.CalledProcessError):
        helper()


@pytest.fixture(scope="module")
def private_pem():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=3072)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )


@pytest.mark.parametrize("fault", [None, "missing", "active", "origin", "spool", "access", "path"])
def test_runtime_probe_checks_real_configuration(
    helper, monkeypatch, settings, tmp_path, private_pem, fault
):
    credential = tmp_path / "report-transfer-keyring"
    credential.write_text(json.dumps({"keys": {"v2": private_pem}}))
    settings.REPORT_TRANSFER_KEYRING_PATH = credential
    settings.REPORT_TRANSFER_ACTIVE_KEY = "v2"
    settings.REPORT_TRANSFER_ROOT = "/var/spool/firedash-reports"
    settings.FIREDASH_PUBLIC_ORIGIN = "https://fire.test"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    monkeypatch.setattr("os.access", lambda *_: fault != "access")
    monkeypatch.setattr(sys, "argv", ["helper", "runtime", "v2", "https://fire.test"])
    if fault == "missing":
        credential.unlink()
    elif fault == "active":
        settings.REPORT_TRANSFER_ACTIVE_KEY = "other"
    elif fault == "origin":
        settings.FIREDASH_PUBLIC_ORIGIN = "https://other.test"
    elif fault == "spool":
        settings.REPORT_TRANSFER_ROOT = Path("/var/lib/fire-backend/reports")
    elif fault == "path":
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "other"))
    if fault:
        with pytest.raises((ValueError, KeyError)):
            helper()
    else:
        helper()
