"""Credential-free retention of private import staging sources."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.ingestion.models import ImportBatch


def cleanup_expired_staging(*, now=None) -> int:
    now = now or timezone.now()
    preview_cutoff = now - timedelta(days=settings.IMPORT_PREVIEW_RETENTION_DAYS)
    applied_cutoff = now - timedelta(days=settings.IMPORT_APPLIED_SOURCE_RETENTION_DAYS)
    batches = ImportBatch.objects.filter(
        Q(
            status__in=(
                ImportBatch.Status.UPLOADED,
                ImportBatch.Status.PREVIEW_READY,
                ImportBatch.Status.INVALID,
                ImportBatch.Status.CANCELLED,
            ),
            created_at__lt=preview_cutoff,
        )
        | Q(status=ImportBatch.Status.APPLIED, applied_at__lt=applied_cutoff)
    )
    removed = 0
    root = settings.INGESTION_STAGING_ROOT.resolve()
    for batch in batches.iterator():
        sanitized_keys = [
            row.get("sanitized_staging_key", "")
            for row in batch.normalized_intent.get("rows", [])
            if isinstance(row, dict)
        ]
        for key in [
            batch.staging_key,
            *sanitized_keys,
        ]:
            candidate = (root / key).resolve()
            if candidate.parent == root:
                candidate.unlink(missing_ok=True)
        if batch.status != ImportBatch.Status.APPLIED:
            batch.status = ImportBatch.Status.EXPIRED
            batch.save(update_fields=("status",))
        removed += 1
    return removed
