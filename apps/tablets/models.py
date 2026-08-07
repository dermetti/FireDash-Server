import uuid

from django.db import models

from apps.organizations.models import Department


class Tablet(models.Model):
    """Assignment-only identity anchor; tablet lifecycle arrives in Phase 8."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="tablet_identities"
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
