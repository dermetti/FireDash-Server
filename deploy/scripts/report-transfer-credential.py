"""Installer-only RSA provisioning/validation. Never emit credential material."""

import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings


def require(condition):
    if not condition:
        raise ValueError("invalid report deployment configuration")


def main():
    operation = sys.argv[1]
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
        key = serialization.load_pem_public_key(config["public_key_pem"].encode("ascii"))
        require(isinstance(key, rsa.RSAPublicKey) and key.key_size >= 3072)
        from PIL import Image

        Image.init()
        require(Image.registered_extensions()[".jpg"] == "JPEG")
        return

    path, version = Path(sys.argv[2]), sys.argv[3]
    if operation == "generate":
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pem = key.private_bytes(
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
