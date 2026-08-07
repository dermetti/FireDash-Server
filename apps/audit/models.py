import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Department, Station


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    actor_installation_uuid = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=128)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.PROTECT)
    target_type = models.CharField(max_length=128)
    target_uuid = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField()
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        db_table = "audit_event"
        ordering = ("-timestamp",)
