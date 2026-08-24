import uuid

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError

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
    street = models.CharField(max_length=255, blank=True)
    house_number = models.CharField(max_length=32, blank=True)
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
    external_identifier = models.CharField(max_length=255, blank=True)
    object_name = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=255, blank=True)
    # Optional descriptive operational metadata. Missing values remain blank
    # canonically and are emitted as JSON null in the publication bundle.
    fsd_location = models.TextField(blank=True)
    bmz_location = models.TextField(blank=True)
    rwa_info = models.TextField(blank=True)
    location = models.PointField(srid=4326, null=True, blank=True)
    document_key = models.CharField(max_length=255, unique=True)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()
    page_count = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    source_pdf_sha256 = models.CharField(max_length=64, blank=True)
    active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_fire_plans"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("department", "active"))]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(external_identifier="") | ~models.Q(address=""),
                name="fire_plan_requires_external_identifier_or_address",
            ),
            models.UniqueConstraint(
                fields=("department", "external_identifier"),
                condition=~models.Q(external_identifier=""),
                name="unique_fire_plan_external_identifier_per_department",
            ),
            models.UniqueConstraint(
                fields=("department", "address"),
                condition=models.Q(external_identifier="") & ~models.Q(address=""),
                name="unique_fire_plan_address_identity_per_department",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.external_identifier = self.external_identifier.strip()
        self.address = self.address.strip()
        if not self.external_identifier and not self.address:
            raise ValidationError(
                {"address": "Address is required when no external identifier is available."}
            )

    @property
    def display_label(self) -> str:
        return self.object_name or self.address or self.external_identifier


class KlgvPlan(models.Model):
    """Canonical source for an optional department KLGV PDF bundle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="klgv_plans")
    external_identifier = models.CharField(max_length=255, blank=True)
    object_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    postal_code = models.CharField(max_length=32)
    city = models.CharField(max_length=255)
    location = models.PointField(srid=4326, null=True, blank=True)
    path = models.CharField(max_length=255, unique=True)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()
    page_count = models.PositiveIntegerField()
    source_pdf_sha256 = models.CharField(max_length=64)
    sha256 = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_klgv_plans"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "external_identifier"),
                condition=~models.Q(external_identifier=""),
                name="unique_klgv_plan_external_identifier_per_department",
            )
        ]
        indexes = [models.Index(fields=("department", "active"))]

    def clean(self) -> None:
        super().clean()
        self.external_identifier = self.external_identifier.strip()
        self.object_name = self.object_name.strip()
        self.address = self.address.strip()
        self.postal_code = self.postal_code.strip()
        self.city = self.city.strip()
        for field in ("object_name", "address", "postal_code", "city"):
            if not getattr(self, field):
                raise ValidationError({field: "This field is required."})


class HydrantImportPreview(models.Model):
    """Short-lived normalized import state; raw GeoJSON is intentionally not retained."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    normalized_features = models.JSONField()
    duplicate_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
