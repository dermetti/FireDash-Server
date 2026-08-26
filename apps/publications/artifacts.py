"""Worker-only encrypted publication artifact creation and lifecycle cleanup."""

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.utils import timezone

from apps.publications.paths import publication_artifact_relative_path

try:
    import grp as _grp
except ImportError:  # pragma: no cover - unavailable on Windows development hosts.
    _grp = cast(Any, None)

grp: Any = _grp

logger = logging.getLogger(__name__)


class ArtifactError(ValueError):
    pass


def _credential(path: Path, label: str) -> bytes:
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ArtifactError(f"Publication {label} credential is unavailable.") from error
    # A raw 32-byte file is unambiguous key material; read it exactly (no strip).
    if len(value) == 32:
        return value
    # LoadCredential files may otherwise hold base64 text (possibly with a newline).
    value = value.strip()
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded if decoded else value
    except ValueError:
        return value


def _fire_nginx_gid() -> int | None:
    if grp is None:
        return None
    try:
        return cast(int, grp.getgrnam("fire_nginx").gr_gid)
    except KeyError as error:
        raise ArtifactError("The fire_nginx group is unavailable.") from error


def _set_final_artifact_permissions(path: Path) -> None:
    group_id = _fire_nginx_gid()
    if group_id is None or not hasattr(os, "chown"):
        return
    os.chown(path, -1, group_id)
    os.chmod(path, 0o640)


def _set_final_directory_permissions(path: Path) -> None:
    """Make a final nested directory traversable/readable by the serving group.

    The worker runs with ``UMask=0077`` so ``mkdir(mode=0o750)`` still yields
    ``0700``; the group must be re-applied and the mode set explicitly so Nginx
    (group ``fire_nginx``) can traverse the directory to reach ``artifact.bin``.
    """
    group_id = _fire_nginx_gid()
    if group_id is None or not hasattr(os, "chown"):
        return
    os.chown(path, -1, group_id)
    # 0750 grants only group read/traverse for the serving group; no group write.
    os.chmod(path, 0o750)  # nosec B103


def _signature_payload(
    *, publication, wrapped_cek: bytes, nonce: bytes, ciphertext: bytes
) -> bytes:
    return json.dumps(
        {
            "wrapped_cek": base64.b64encode(wrapped_cek).decode("ascii"),
            "wrapping_algorithm": "AES-KW-RFC3394",
            "encryption_algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "ciphertext_size": len(ciphertext),
            "schema_version": publication.schema_version,
            "version_number": publication.version_number,
            "scope": {
                "department_id": str(publication.department_id),
                "station_id": str(publication.station_id) if publication.station_id else None,
                "dataset_type_code": publication.dataset_type_code,
            },
            "kek_version": settings.PUBLICATION_KEK_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_encrypted_artifact(*, publication, plaintext: bytes) -> dict[str, object]:
    """Encrypt, sign, and atomically promote a worker-owned artifact."""
    if len(plaintext) > settings.PUBLICATION_ARTIFACT_MAX_BYTES:
        raise ArtifactError("Artifact exceeds the configured size limit.")
    kek = _credential(settings.PUBLICATION_KEK_CREDENTIAL_PATH, "KEK")
    if len(kek) != 32:
        raise ArtifactError("Publication KEK must be exactly 32 bytes.")
    signing_key = _credential(settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH, "signing")
    if len(signing_key) != 32:
        raise ArtifactError("Publication Ed25519 private key must be exactly 32 bytes.")
    cek, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext, None)
    wrapped_cek = keywrap.aes_key_wrap(kek, cek)
    signature = Ed25519PrivateKey.from_private_bytes(signing_key).sign(
        _signature_payload(
            publication=publication, wrapped_cek=wrapped_cek, nonce=nonce, ciphertext=ciphertext
        )
    )
    # The canonical final path is <department_id>/<publication_id>/artifact.bin.
    relative_path = publication_artifact_relative_path(
        department_id=publication.department_id, publication_id=publication.id
    )
    final_path = settings.PUBLICATION_ARTIFACT_ROOT / relative_path
    temp_dir = settings.PUBLICATION_ARTIFACT_TEMP_ROOT / str(publication.id)
    try:
        temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_path = temp_dir / "artifact.bin"
        with temp_path.open("xb") as artifact_file:
            artifact_file.write(ciphertext)
            artifact_file.flush()
            os.fsync(artifact_file.fileno())
        final_path.parent.mkdir(parents=True, exist_ok=True)
        _set_final_directory_permissions(final_path.parent)
        _set_final_directory_permissions(final_path.parent.parent)
        os.replace(temp_path, final_path)
        _set_final_artifact_permissions(final_path)
    except OSError as error:
        # Log the underlying filesystem error (errno/strerror + paths) without any
        # secret key material or plaintext so it is diagnosable from the worker journal.
        logger.error(
            "Publication artifact promotion failed for %s: %s",
            publication.id,
            error,
        )
        raise ArtifactError("Could not promote publication artifact.") from error
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return {
        "artifact_path": relative_path,
        "artifact_size": len(ciphertext),
        "artifact_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "artifact_nonce": nonce,
        "artifact_wrapped_cek": wrapped_cek,
        "artifact_encryption_algorithm": "AES-256-GCM",
        "artifact_wrapping_algorithm": "AES-KW-RFC3394",
        "artifact_kek_version": settings.PUBLICATION_KEK_VERSION,
        "artifact_signature": signature,
        "artifact_signature_algorithm": "Ed25519",
        "artifact_signing_key_version": settings.PUBLICATION_SIGNING_KEY_VERSION,
    }


def remove_artifact(publication) -> None:
    remove_artifact_path(publication.artifact_path)


def remove_artifact_path(relative_path: object) -> None:
    if isinstance(relative_path, str) and relative_path:
        root = settings.PUBLICATION_ARTIFACT_ROOT.resolve()
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ArtifactError("Artifact path must remain below the artifact root.") from error
        path.unlink(missing_ok=True)
        # Best-effort housekeeping: remove the now-empty publication directory.
        # rmdir only succeeds when empty, so this never deletes sibling artifacts.
        try:
            path.parent.rmdir()
        except OSError:
            pass


def cleanup_stale_artifacts() -> int:
    """Remove abandoned temp dirs and files belonging to failed/obsolete builds."""
    cutoff = timezone.now() - timedelta(seconds=settings.PUBLICATION_ARTIFACT_STALE_SECONDS)
    removed = 0
    for path in (
        settings.PUBLICATION_ARTIFACT_TEMP_ROOT.glob("*")
        if settings.PUBLICATION_ARTIFACT_TEMP_ROOT.exists()
        else ()
    ):
        if path.is_dir() and path.stat().st_mtime < cutoff.timestamp():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    from apps.publications.models import DatasetPublication

    for publication in DatasetPublication.objects.filter(
        status__in=("FAILED", "OBSOLETE", "REJECTED", "CANCELLED"), artifact_path__gt=""
    ):
        try:
            remove_artifact(publication)
        except (ArtifactError, OSError) as error:
            logger.warning(
                "Publication artifact cleanup deferred for %s: %s", publication.id, error
            )
            continue
        # A READY artifact's signed metadata is immutable. Its terminal
        # publication state prevents future distribution, so preserve the
        # historical path/metadata even after ciphertext cleanup. Non-ready
        # temporary records retain the legacy path-clearing behavior.
        if publication.artifact_status != DatasetPublication.ArtifactStatus.READY:
            DatasetPublication.objects.filter(pk=publication.pk).update(artifact_path="")
        removed += 1
    return removed
