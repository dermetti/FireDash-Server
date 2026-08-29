import uuid

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError

from apps.organizations.models import Department, Station


class Hydrant(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        UNKNOWN = "UNKNOWN", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="hydrants")
    external_identifier = models.CharField(max_length=255, blank=True)
    geometry = models.PointField(srid=4326)
    street = models.CharField(max_length=255, blank=True)
    house_number = models.CharField(max_length=32, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
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


class PhonebookEntry(models.Model):
    """A canonical, department-owned operational telephone directory entry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="phonebook_entries"
    )
    station = models.ForeignKey(
        Station, null=True, blank=True, on_delete=models.PROTECT, related_name="phonebook_entries"
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    organization_unit = models.CharField(max_length=255, blank=True)
    function = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("department", "station"))]

    def clean(self) -> None:
        super().clean()
        for field in ("first_name", "last_name", "organization_unit", "function", "phone_number"):
            setattr(self, field, (getattr(self, field) or "").strip())
        if bool(self.first_name) != bool(self.last_name):
            raise ValidationError({"last_name": "Provide both first and last name."})
        if not (self.first_name and self.last_name) and not self.organization_unit:
            raise ValidationError(
                {"organization_unit": "Provide a complete name or an organization unit."}
            )
        if self.station_id and self.station.department_id != self.department_id:
            raise ValidationError({"station": "Station must belong to the selected department."})

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.organization_unit

    @property
    def scope_label(self) -> str:
        return self.station.name if self.station_id else "Department"


class PhonebookDuplicateDecision(models.Model):
    """A keep-both decision is valid only for the exact reviewed entry revisions."""

    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name="phonebook_duplicate_decisions"
    )
    first_entry = models.ForeignKey(
        PhonebookEntry, on_delete=models.CASCADE, related_name="duplicate_decisions_as_first"
    )
    second_entry = models.ForeignKey(
        PhonebookEntry, on_delete=models.CASCADE, related_name="duplicate_decisions_as_second"
    )
    first_fingerprint = models.CharField(max_length=64)
    second_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("first_entry", "second_entry"), name="unique_phonebook_duplicate_pair"
            ),
            models.CheckConstraint(
                condition=~models.Q(first_entry=models.F("second_entry")),
                name="phonebook_duplicate_entries_differ",
            ),
        ]


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
