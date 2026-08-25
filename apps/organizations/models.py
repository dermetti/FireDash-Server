import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.organizations.presentation import (
    DEFAULT_DEPARTMENT_LOCALE,
    DEFAULT_DEPARTMENT_TIMEZONE,
    DEPARTMENT_LOCALE_CHOICES,
    DEPARTMENT_TIMEZONE_CHOICES,
)


class Department(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        DEACTIVATED = "DEACTIVATED", "Deactivated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    short_code = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    tablet_lease_days = models.PositiveSmallIntegerField(
        default=7, validators=[MinValueValidator(3)]
    )
    tablet_asset_number_auto_enabled = models.BooleanField(default=False)
    tablet_asset_number_prefix = models.CharField(max_length=128, blank=True, default="")
    tablet_asset_number_width = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    # This is the authoritative numeric allocator state.  It is deliberately
    # separate from the presentation prefix and zero-padding width.
    tablet_asset_number_sequence = models.PositiveBigIntegerField(default=0)
    locale = models.CharField(
        max_length=8,
        choices=DEPARTMENT_LOCALE_CHOICES,
        default=DEFAULT_DEPARTMENT_LOCALE,
    )
    timezone = models.CharField(
        max_length=64,
        choices=DEPARTMENT_TIMEZONE_CHOICES,
        default=DEFAULT_DEPARTMENT_TIMEZONE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_departments",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tablet_lease_days__gte=3),
                name="department_tablet_lease_days_min",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    tablet_asset_number_width__gte=1,
                    tablet_asset_number_width__lte=20,
                ),
                name="department_tablet_asset_number_width_range",
            ),
            models.CheckConstraint(
                condition=models.Q(tablet_asset_number_sequence__gte=0),
                name="department_tablet_asset_number_sequence_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Station(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="stations")
    name = models.CharField(max_length=255)
    short_code = models.CharField(max_length=64)
    street = models.CharField(max_length=255, blank=True)
    house_number = models.CharField(max_length=32, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Vehicle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="vehicles")
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="vehicles")
    display_name = models.CharField(max_length=255)
    call_sign = models.CharField(max_length=128, blank=True)
    asset_identifier = models.CharField(max_length=128, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.display_name
