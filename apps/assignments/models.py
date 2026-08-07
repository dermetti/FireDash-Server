import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Station, Vehicle
from apps.personnel.models import Person
from apps.tablets.models import Tablet


class PersonnelStationAssignment(models.Model):
    class AssignmentType(models.TextChoices):
        HOME = "HOME", "Home"
        TEMPORARY = "TEMPORARY", "Temporary"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="station_assignments")
    station = models.ForeignKey(
        Station, on_delete=models.PROTECT, related_name="personnel_assignments"
    )
    assignment_type = models.CharField(max_length=16, choices=AssignmentType.choices)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=512, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_personnel_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_personnel_assignments",
    )
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("person",),
                condition=Q(
                    assignment_type="HOME", valid_until__isnull=True, ended_at__isnull=True
                ),
                name="one_open_home_assignment_per_person",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="personnel_assignment_valid_window",
            ),
        ]


class TabletVehicleAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tablet = models.ForeignKey(Tablet, on_delete=models.PROTECT, related_name="vehicle_assignments")
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name="tablet_assignments"
    )
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_tablet_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_tablet_assignments",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=512, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tablet",),
                condition=Q(valid_until__isnull=True, ended_at__isnull=True),
                name="one_open_vehicle_assignment_per_tablet",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="tablet_assignment_valid_window",
            ),
        ]
