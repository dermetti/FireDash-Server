"""Private ciphertext-only spool. Never a Django public/media storage backend."""

import os
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.db import connection

from apps.report_transfer.contract import TransferError


def lock_spool():
    # Acceptance and orphan sweeping share this transaction lock. No sweeper can
    # mistake an in-flight file for an orphan, even after a long process pause.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [7361031001])


def root():
    path = Path(settings.REPORT_TRANSFER_ROOT).resolve()
    for setting in (
        "PUBLICATION_ARTIFACT_ROOT",
        "MEDIA_ROOT",
        "INGESTION_STAGING_ROOT",
        "REFERENCE_DATA_ACCEPTED_ROOT",
    ):
        other = getattr(settings, setting, None)
        if other:
            other = Path(other).resolve()
            if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                raise TransferError("report_storage_unavailable", 503)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def payload_path(key):
    try:
        if key != f"{UUID(key.removesuffix('.jwe'))}.jwe":
            raise ValueError
    except (ValueError, AttributeError):
        raise TransferError("report_storage_unavailable", 503) from None
    path = root() / key
    if path.is_symlink():
        raise TransferError("report_storage_unavailable", 503)
    return path


def write_payload(raw):
    key = f"{uuid4()}.jwe"
    path = payload_path(key)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return key


def remove_payload(key):
    payload_path(key).unlink(missing_ok=True)
