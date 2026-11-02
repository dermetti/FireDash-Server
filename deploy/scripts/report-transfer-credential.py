"""Installer-only RSA provisioning/validation. Never emit credential material."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings


def require(condition):
    if not condition:
        raise ValueError("invalid report deployment configuration")


def inspect_unit(mode: str, text: str) -> None:
    """Check effective systemctl-cat declarations without printing their values.

    systemd exposes complex D-Bus properties as [unprintable] on some releases.
    The caller first requires NeedDaemonReload=no, then supplies the fragment and
    drop-ins in systemd's order. List resets and line continuations are honored.
    Unsupported/ambiguous escapes and specifiers fail closed for inspected values.
    """
    section = ""
    pending = ""
    values: dict[str, list[str]] = {}
    sections = {"web": "Service", "worker": "Service", "expiry": "Service", "timer": "Timer"}
    require(mode in sections)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != sections[mode] or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in {
            "LoadCredential",
            "LoadCredentialEncrypted",
            "SetCredential",
            "SetCredentialEncrypted",
            "ImportCredential",
            "ReadWritePaths",
            "ExecStart",
            "Environment",
            "EnvironmentFile",
            "OnBootSec",
            "OnUnitActiveSec",
            "OnActiveSec",
            "OnStartupSec",
            "OnUnitInactiveSec",
            "OnCalendar",
            "Unit",
            "Persistent",
        }:
            continue
        if not value:
            values[name] = []
        else:
            require("\\" not in value and "%" not in value)
            # Commands retain their argv grouping; the others are word lists.
            if name in {"Unit", "Persistent"}:
                values[name] = shlex.split(value)
            elif name == "ExecStart":
                values.setdefault(name, []).append(value)
            else:
                values.setdefault(name, []).extend(shlex.split(value))
    require(not pending and bool(section))
    report_name = "report-transfer-keyring"
    report_path = "/etc/fire-backend/credentials/report-transfer-keyring.json"
    expected = f"{report_name}:{report_path}"
    loads = values.get("LoadCredential", [])
    report_loads = [v for v in loads if v.split(":", 1)[0] == report_name or report_path in v]
    require(report_loads == ([expected] if mode == "web" else []))
    for directive in (
        "LoadCredentialEncrypted",
        "SetCredential",
        "SetCredentialEncrypted",
        "ImportCredential",
    ):
        require(
            not any(
                report_name in v
                or report_path in v
                or (directive == "ImportCredential" and any(c in v for c in "*?["))
                for v in values.get(directive, [])
            )
        )
    if mode in {"web", "expiry"}:
        writes = values.get("ReadWritePaths", [])
        if mode == "web":
            require("/var/spool/firedash-reports" in writes and "/var/lib/fire-backend" in writes)
        else:
            require(writes == ["/var/spool/firedash-reports"])
            commands = values.get("ExecStart", [])
            require(
                len(commands) == 1
                and shlex.split(commands[0])
                == [
                    "/srv/firedash/current/venv/bin/python",
                    "manage.py",
                    "expire_report_submissions",
                ]
            )
            environment = dict(v.split("=", 1) for v in values.get("Environment", []))
            require(environment.get("DJANGO_SETTINGS_MODULE") == "config.settings.production")
            require(values.get("EnvironmentFile") == ["/etc/fire-backend/fire-backend.env"])
    if mode == "timer":
        five_minutes = {"5min", "5m", "300s", "300sec", "300000ms", "300000000us"}
        require(len(values.get("OnBootSec", [])) == 1 and values["OnBootSec"][0] in five_minutes)
        require(
            len(values.get("OnUnitActiveSec", [])) == 1
            and values["OnUnitActiveSec"][0] in five_minutes
        )
        for directive in ("OnActiveSec", "OnStartupSec", "OnUnitInactiveSec", "OnCalendar"):
            require(not values.get(directive))
        require(values.get("Unit") == ["fire-report-expiry.service"])
        require(values.get("Persistent") in (["true"], ["yes"], ["1"], ["on"]))


def main():
    operation = sys.argv[1]
    if operation == "inspect-unit":
        inspect_unit(sys.argv[2], sys.stdin.read())
        return
    if operation == "probe":
        pid, release = sys.argv[2], Path(sys.argv[3])
        require(pid.isdecimal() and int(pid) > 0)
        environment = dict(
            entry.decode().split("=", 1)
            for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if entry
        )
        require(environment["DJANGO_SETTINGS_MODULE"] == "config.settings.production")
        require(bool(environment["CREDENTIALS_DIRECTORY"]))
        environment["PYTHONPATH"] = str(release)
        # Enter the running service's credential mount namespace; privilege is
        # dropped before invoking the application parser. No credentials in argv.
        subprocess.run(
            [
                "nsenter",
                "--target",
                pid,
                "--mount",
                "--",
                "runuser",
                "-u",
                "fire_backend",
                "--",
                str(release / "venv/bin/python"),
                str(release / "deploy/scripts/report-transfer-credential.py"),
                "runtime",
                sys.argv[4],
                sys.argv[5],
            ],
            env=environment,
            cwd=release,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if operation == "runtime":
        from apps.report_transfer.contract import configuration

        config = configuration()
        require(
            settings.REPORT_TRANSFER_KEYRING_PATH
            == (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "report-transfer-keyring")
        )
        require(os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.production")
        require(str(settings.REPORT_TRANSFER_ROOT) == "/var/spool/firedash-reports")
        require(os.access(settings.REPORT_TRANSFER_ROOT, os.R_OK | os.W_OK | os.X_OK))
        require(config["enabled"] is True and config["version"] == 1)
        require(config["kid"] == settings.REPORT_TRANSFER_ACTIVE_KEY == sys.argv[2])
        require(settings.FIREDASH_PUBLIC_ORIGIN == sys.argv[3])
        require(config["alg"] == "RSA-OAEP-256" and config["enc"] == "A256GCM")
        require(config["max_attachments"] > 0 and config["max_jpeg_bytes"] > 0)
        public_key = serialization.load_pem_public_key(config["public_key_pem"].encode("ascii"))
        require(isinstance(public_key, rsa.RSAPublicKey) and public_key.key_size >= 3072)
        from PIL import Image

        Image.init()
        require(Image.registered_extensions()[".jpg"] == "JPEG")
        return

    path, version = Path(sys.argv[2]), sys.argv[3]
    if operation == "generate":
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        # Caller supplies an existing protected staging file, not a destination.
        path.write_text(json.dumps({"keys": {version: pem}}) + "\n", encoding="ascii")
    elif operation != "validate":
        raise ValueError("unsupported operation")

    settings.configure(REPORT_TRANSFER_KEYRING_PATH=path, REPORT_TRANSFER_ACTIVE_KEY=version)
    from apps.report_transfer.contract import private_keys

    keys = private_keys()  # Same duplicate-JSON, PEM, version and RSA checks as runtime.
    keys[version].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Neither exception messages nor traceback locals may expose credentials.
        raise SystemExit("report_transfer_configuration_invalid") from None
