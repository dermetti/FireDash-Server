from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.maintenance import cleanup_expired_staging
from apps.ingestion.models import ImportBatch
from apps.ingestion.storage import stage_upload
from apps.organizations.models import Department


@pytest.mark.django_db(transaction=True)
def test_credential_free_cleanup_expires_preview_staging_without_canonical_mutation(
    settings, tmp_path
):
    settings.INGESTION_STAGING_ROOT = tmp_path / "private-import-staging"
    actor = User.objects.create_user("cleanup-admin@example.test", "Cleanup Admin", "safe-password")
    department = Department.objects.create(name="Cleanup", short_code="CLN", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    batch = ImportBatch.objects.create(
        domain=ImportBatch.Domain.HYDRANTS,
        department=department,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        original_filename="old.csv",
        upload_sha256="0" * 64,
        staging_key="pending-cleanup.source",
        actor=actor,
        status=ImportBatch.Status.PREVIEW_READY,
    )
    key, digest = stage_upload(batch_id=batch.id, payload=b"old preview")
    batch.staging_key = key
    batch.upload_sha256 = digest
    batch.created_at = timezone.now() - timedelta(days=settings.IMPORT_PREVIEW_RETENTION_DAYS + 1)
    batch.save(update_fields=("staging_key", "upload_sha256", "created_at"))

    assert cleanup_expired_staging() == 1
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.EXPIRED
    assert not (settings.INGESTION_STAGING_ROOT / key).exists()
