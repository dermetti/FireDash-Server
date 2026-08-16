"""Private import staging.  Staged bytes are never served by Nginx."""

import hashlib
import os

from django.conf import settings


class ImportStorageError(ValueError):
    pass


def stage_upload(*, batch_id, payload: bytes) -> tuple[str, str]:
    if len(payload) > settings.MAX_PDF_PACKAGE_BYTES:
        raise ImportStorageError("Import exceeds the configured upload limit.")
    root = settings.INGESTION_STAGING_ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = f"{batch_id}.source"
    target = root / key
    if target.exists():
        raise ImportStorageError("Import staging target already exists.")
    temporary = root / f".{key}.tmp"
    with temporary.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return key, hashlib.sha256(payload).hexdigest()


def read_staged(*, key: str) -> bytes:
    root = settings.INGESTION_STAGING_ROOT.resolve()
    path = (root / key).resolve()
    if path.parent != root or not path.is_file():
        raise ImportStorageError("Preview source is unavailable.")
    return path.read_bytes()


def remove_staged(*, key: str) -> None:
    """Remove a single validated private staging object, if it still exists."""
    root = settings.INGESTION_STAGING_ROOT.resolve()
    path = (root / key).resolve()
    if path.parent == root:
        path.unlink(missing_ok=True)
