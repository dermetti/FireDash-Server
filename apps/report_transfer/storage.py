"""Private ciphertext-only spool. Never a Django public/media storage backend."""

import json
import os
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.db import connection
from django.utils import timezone

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


def write_download_receipt(submission_pk, completed_at):
    """Crash-recoverable completion metadata only; never content or bearer data."""
    path = root() / f"{submission_pk}-{uuid4()}.receipt"
    temporary = path.with_suffix(".receipt-tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        json.dump(
            {"submission_pk": submission_pk, "completed_at": completed_at.isoformat()}, output
        )
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return path


def read_download_receipt(path):
    try:
        if path.is_symlink() or path.stat().st_size > 256:
            raise ValueError
        receipt = json.loads(path.read_text(encoding="ascii"))
        pk = receipt["submission_pk"]
        completed_at = datetime.fromisoformat(receipt["completed_at"])
        if (
            type(pk) is not int
            or pk <= 0
            or not path.name.startswith(f"{pk}-")
            or timezone.is_naive(completed_at)
            or completed_at > timezone.now()
        ):
            raise ValueError
        return pk, completed_at
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise TransferError("report_storage_unavailable", 503) from None


def pending_downloads(submission_pk):
    # A read must never provision storage, especially in status-only requests.
    directory = Path(settings.REPORT_TRANSFER_ROOT)
    timestamps = []
    for path in directory.glob(f"{submission_pk}-*.receipt"):
        try:
            timestamps.append(read_download_receipt(path)[1])
        except FileNotFoundError:
            # A concurrent successful database reconciliation removed it.
            continue
    return timestamps
