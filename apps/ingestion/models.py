import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Department, Station


class ImportBatch(models.Model):
    class Domain(models.TextChoices):
        HYDRANTS = "hydrants", "Hydrants"
        PERSONNEL = "personnel", "Personnel"
        FIRE_PLANS = "fire_plans", "Fire plans"
        KLGV_PLANS = "klgv_plans", "KLGV plans"
        STATION_VEHICLES = "station_vehicles", "Stations and vehicles"
        PHONEBOOK = "phonebook", "Phonebook"
        DANGEROUS_GOODS = "dangerous_goods", "Dangerous goods"

    class Format(models.TextChoices):
        CSV = "csv", "CSV"
        JSON = "json", "JSON"
        GEOJSON = "geojson", "GeoJSON"
        PDF = "pdf", "PDF"
        ZIP = "zip", "ZIP"

    class Mode(models.TextChoices):
        MERGE = "merge", "Merge"
        AUTHORITATIVE_SNAPSHOT = "authoritative_snapshot", "Authoritative snapshot"
        UPSERT = "upsert", "Upsert"

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        PREVIEW_READY = "PREVIEW_READY", "Preview ready"
        INVALID = "INVALID", "Invalid"
        APPLIED = "APPLIED", "Applied"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    domain = models.CharField(max_length=32, choices=Domain.choices)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="import_batches"
    )
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.PROTECT)
    import_format = models.CharField(max_length=16, choices=Format.choices)
    import_mode = models.CharField(max_length=32, choices=Mode.choices)
    original_filename = models.CharField(max_length=255)
    upload_sha256 = models.CharField(max_length=64)
    staging_key = models.CharField(max_length=255, unique=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    baseline = models.JSONField(default=dict)
    normalized_intent = models.JSONField(default=dict)
    validation_errors = models.JSONField(default=list)
    validation_summary = models.JSONField(default=dict)
    add_count = models.PositiveIntegerField(default=0)
    update_count = models.PositiveIntegerField(default=0)
    deactivate_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    affected_scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("department", "status", "created_at"))]
