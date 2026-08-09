import uuid

from django.conf import settings
from django.contrib.gis.db import models

from apps.organizations.models import Department


class Hydrant(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        UNKNOWN = "UNKNOWN", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="hydrants")
    external_identifier = models.CharField(max_length=255, blank=True)
    location = models.PointField(srid=4326)
    hydrant_type = models.CharField(max_length=128, blank=True)
    flow_information = models.CharField(max_length=255, blank=True)
    diameter_mm = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=128, blank=True, default="ACTIVE")
    source_metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "external_identifier"),
                condition=~models.Q(external_identifier=""),
                name="unique_hydrant_external_identifier_per_department",
            )
        ]
        indexes = [models.Index(fields=("department", "status"))]

    @property
    def active(self) -> bool:
        return self.status == self.Status.ACTIVE


class FirePlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="fire_plans")
    object_name = models.CharField(max_length=255)
    object_reference = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True)
    location = models.PointField(srid=4326, null=True, blank=True)
    document_key = models.CharField(max_length=255, unique=True)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()
    page_count = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_fire_plans"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("department", "active"))]


class HydrantImportPreview(models.Model):
    """Short-lived normalized import state; raw GeoJSON is intentionally not retained."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    normalized_features = models.JSONField()
    duplicate_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
