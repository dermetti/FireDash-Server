"""Persistent local fake-device state.

A physical iPad persists a small amount of state for the lifetime of an
installation: the installation UUID, the P-256 HPKE key pair, the issued
installation credential, the app version, and the provisioned server origin.
This module mirrors exactly that, using an owner-only local directory.

The state directory must never be committed to source control.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from tools.fake_ipad.crypto import CryptoState
from tools.fake_ipad.errors import fail

STATE_FILENAME = "state.json"
PRIVATE_KEY_FILENAME = "p256-private-key.pem"
MANIFEST_FILENAME = "last-verified-manifest.json"

DEFAULT_STATE_DIR = ".firedash-fake-ipad"


def secure_write(path: Path, data: bytes) -> None:
    """Write ``data`` atomically with owner-only (0600) permissions where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def secure_write_json(path: Path, value: Any) -> None:
    secure_write(
        path,
        (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )


class DeviceState:
    """Load, persist, and reset the fake installation's local identity/credential."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_path = state_dir / STATE_FILENAME
        self.private_key_path = state_dir / PRIVATE_KEY_FILENAME
        self.manifest_path = state_dir / MANIFEST_FILENAME
        self.state: dict[str, Any] = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text("utf-8"))

    # --- identity ---------------------------------------------------------

    def _get(self, key: str) -> Any:
        return self.state.get(key)

    def _set(self, key: str, value: Any) -> None:
        self.state[key] = value

    @property
    def server_url(self) -> str | None:
        # ``base_url`` is the legacy key; ``server_url`` is the current one.
        value = self.state.get("server_url") or self.state.get("base_url")
        return value if isinstance(value, str) and value else None

    @property
    def app_version(self) -> str:
        value = self.state.get("app_version")
        return value if isinstance(value, str) and value else "1.0.0"

    @property
    def app_build(self) -> int | None:
        value = self.state.get("app_build")
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        )

    @property
    def server_time(self) -> str | None:
        value = self.state.get("server_time")
        return value if isinstance(value, str) and value else None

    @property
    def installation_uuid(self) -> str:
        value = self.state.get("installation_uuid")
        if not isinstance(value, str) or not value:
            fail("No stored installation UUID. Run 'adopt' first.")
        return value

    @property
    def installation_id(self) -> str | None:
        value = self.state.get("installation_id")
        return value if isinstance(value, str) else None

    @property
    def tablet_id(self) -> str | None:
        value = self.state.get("tablet_id")
        return value if isinstance(value, str) else None

    @property
    def credential(self) -> str:
        value = self.state.get("credential")
        if not isinstance(value, str) or not value:
            fail("No stored installation credential. Run 'adopt' first.")
        return value

    @property
    def has_credential(self) -> bool:
        return isinstance(self.state.get("credential"), str) and bool(self.state["credential"])

    @property
    def has_identity(self) -> bool:
        return (
            isinstance(self.state.get("installation_uuid"), str) and self.private_key_path.exists()
        )

    @property
    def is_adopted(self) -> bool:
        return self.has_identity and self.has_credential

    @property
    def authorization_valid_until(self) -> str | None:
        value = self.state.get("authorization_valid_until")
        return value if isinstance(value, str) else None

    # --- persistence ------------------------------------------------------

    def save(self) -> None:
        secure_write_json(self.state_path, self.state)

    def set_app_identity(self, *, app_version: str, app_build: int | None) -> None:
        """Persist the fake app identity without changing installation identity."""
        self.state["app_version"] = app_version
        if app_build is None:
            self.state.pop("app_build", None)
        else:
            self.state["app_build"] = app_build
        self.save()

    def ensure_identity(
        self, *, server_url: str, app_version: str, app_build: int | None = None
    ) -> CryptoState:
        """Reuse an existing identity, or create and persist one on first run."""
        if self.has_identity:
            return self.load_private_key()

        self.state_dir.mkdir(parents=True, exist_ok=True)
        crypto = CryptoState.generate()
        secure_write(self.private_key_path, crypto.private_pem())

        self.state = {
            "server_url": server_url,
            "app_version": app_version,
            "installation_uuid": str(uuid.uuid4()),
            "public_key_fingerprint": crypto.fingerprint(),
        }
        if app_build is not None:
            self.state["app_build"] = app_build
        self.save()
        return crypto

    def load_private_key(self) -> CryptoState:
        if not self.private_key_path.exists():
            fail("No fake-iPad private key. Run 'adopt' first.")
        return CryptoState.from_pem(self.private_key_path.read_bytes())

    def store_installation(
        self,
        *,
        installation_id: str,
        tablet_id: str,
        credential: str,
        authorization_valid_until: str,
        server_time: str | None = None,
    ) -> None:
        self.state.update(
            {
                "installation_id": installation_id,
                "tablet_id": tablet_id,
                "credential": credential,
                "authorization_valid_until": authorization_valid_until,
            }
        )
        if server_time is not None:
            self.state["server_time"] = server_time
        self.save()

    def rotate_credential(
        self, *, credential: str, authorization_valid_until: str, server_time: str | None = None
    ) -> None:
        self.state["credential"] = credential
        self.state["authorization_valid_until"] = authorization_valid_until
        if server_time is not None:
            self.state["server_time"] = server_time
        self.save()

    def update_lease(
        self, authorization_valid_until: str, *, server_time: str | None = None
    ) -> None:
        self.state["authorization_valid_until"] = authorization_valid_until
        if server_time is not None:
            self.state["server_time"] = server_time
        self.save()

    def cached_signing_key(self, version: str) -> bytes | None:
        keys = self.state.get("signing_keys")
        if not isinstance(keys, dict):
            return None
        encoded = keys.get(version)
        if not isinstance(encoded, str):
            return None
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError):
            return None

    def cache_signing_key(self, version: str, public_key: bytes) -> None:
        keys = self.state.setdefault("signing_keys", {})
        if not isinstance(keys, dict):
            keys = {}
            self.state["signing_keys"] = keys
        keys[version] = base64.b64encode(public_key).decode("ascii")
        self.save()

    # --- reset ------------------------------------------------------------

    def reset(self) -> list[str]:
        """Delete only the local fake-device state (never server-side state)."""
        removed: list[str] = []
        if not self.state_dir.exists():
            return removed
        for child in sorted(self.state_dir.iterdir(), key=lambda p: p.name):
            if child.is_file():
                child.unlink()
                removed.append(child.name)
        return removed

    # --- introspection ----------------------------------------------------

    def local_summary(self) -> dict[str, Any]:
        return {
            "server_url": self.server_url,
            "installation_uuid": self.state.get("installation_uuid"),
            "installation_id": self.installation_id,
            "tablet_id": self.tablet_id,
            "adopted": self.is_adopted,
            "app_version": self.app_version,
            "app_build": self.app_build,
            "credential_present": self.has_credential,
            "authorization_valid_until": self.authorization_valid_until,
            "server_time": self.server_time,
            "cached_signing_key_versions": sorted(
                self.state.get("signing_keys", {})
                if isinstance(self.state.get("signing_keys"), dict)
                else []
            ),
        }
