"""Narrow reconciliation of UUID-addressed accepted library PDFs."""

import hashlib
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.file_library.models import Content


def lock_storage(library_id):
    """Serialize promotion/reconciliation even if library creation rolls back."""
    key = int.from_bytes(
        hashlib.sha256(f"file-library:{library_id}".encode()).digest()[:8], "big", signed=True
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def reconcile_orphans(*, grace=timedelta(days=7), dry_run=False):
    if grace < timedelta(days=1):
        raise ValueError("Orphan cleanup requires at least a one-day grace period.")
    root = (settings.REFERENCE_DATA_ACCEPTED_ROOT / "file-libraries").resolve()
    if not root.exists():
        return 0
    cutoff = (timezone.now() - grace).timestamp()
    removed = 0
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            library_id = UUID(directory.name)
        except ValueError:
            continue
        if str(library_id) != directory.name:
            continue
        # Promotion holds this lock until commit/rollback, so even an old file
        # copied by a long-running transaction cannot be mistaken for an orphan.
        with transaction.atomic():
            lock_storage(library_id)
            for path in directory.glob("*.pdf"):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(root)
                ):
                    continue
                try:
                    content_id = UUID(path.stem)
                except ValueError:
                    continue
                if str(content_id) != path.stem:
                    continue
                key = path.relative_to(settings.REFERENCE_DATA_ACCEPTED_ROOT.resolve()).as_posix()
                if Content.objects.filter(storage_key=key).exists():
                    continue
                try:
                    modified = path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if modified >= cutoff:
                    continue
                if not dry_run:
                    path.unlink(missing_ok=True)
                removed += 1
    return removed
