import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department


class Person(models.Model):
    class LifecycleStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        DEPARTED = "DEPARTED", "Departed"
        ANONYMIZED = "ANONYMIZED", "Anonymized"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="person_identities"
    )
    personnel_number = models.CharField(max_length=128, null=True, blank=True)
    first_name = models.CharField(max_length=128, null=True, blank=True)
    last_name = models.CharField(max_length=128, null=True, blank=True)
    display_name = models.CharField(max_length=255, default="")
    lifecycle_status = models.CharField(
        max_length=16, choices=LifecycleStatus.choices, default=LifecycleStatus.ACTIVE
    )
    active = models.BooleanField(default=True)
    incident_commander_eligible = models.BooleanField(default=False)
    incident_commander_email = models.EmailField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verified_commander_emails",
    )
    departed_at = models.DateTimeField(null=True, blank=True)
    retention_until = models.DateTimeField(null=True, blank=True)
    anonymized_at = models.DateTimeField(null=True, blank=True)
    anonymized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="anonymized_people",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "personnel_number"),
                condition=Q(lifecycle_status="ACTIVE", personnel_number__isnull=False),
                name="unique_active_personnel_number_per_department",
            ),
            models.CheckConstraint(
                condition=(
                    Q(lifecycle_status="ACTIVE", active=True, departed_at__isnull=True)
                    | Q(lifecycle_status="DEPARTED", active=False, departed_at__isnull=False)
                    | Q(lifecycle_status="ANONYMIZED", active=False, anonymized_at__isnull=False)
                ),
                name="person_lifecycle_state_consistency",
            ),
        ]


class PersonnelRetentionPolicy(models.Model):
    department = models.OneToOneField(
        Department, on_delete=models.PROTECT, related_name="personnel_retention_policy"
    )
    retention_period = models.DurationField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="updated_personnel_retention_policies",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(retention_period__gt=timedelta(0)),
                name="personnel_retention_period_positive",
            )
        ]
