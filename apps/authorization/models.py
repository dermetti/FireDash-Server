import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department, Station


class SystemRole(models.Model):
    class Role(models.TextChoices):
        SYSTEM_ADMIN = "SYSTEM_ADMIN", "System administrator"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="system_roles"
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.SYSTEM_ADMIN)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="created_system_roles",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "role"), condition=Q(active=True), name="one_active_system_role"
            ),
            models.CheckConstraint(
                condition=Q(role="SYSTEM_ADMIN"), name="system_role_is_system_admin"
            ),
        ]


class DepartmentMembership(models.Model):
    class Role(models.TextChoices):
        DEPARTMENT_ADMIN = "DEPARTMENT_ADMIN", "Department administrator"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="department_memberships"
    )
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.DEPARTMENT_ADMIN)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_department_memberships",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="revoked_department_memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "department", "role"),
                condition=Q(active=True),
                name="one_active_department_role",
            ),
            models.CheckConstraint(
                condition=Q(role="DEPARTMENT_ADMIN"), name="membership_role_is_department_admin"
            ),
            models.CheckConstraint(
                condition=(
                    Q(active=True, revoked_at__isnull=True, revoked_by__isnull=True)
                    | Q(active=False, revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="department_membership_revocation_state",
            ),
        ]


class StationAdminAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="station_admin_assignments"
    )
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="admin_assignments")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_station_admin_assignments",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="revoked_station_admin_assignments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "station"),
                condition=Q(active=True),
                name="one_active_station_admin_assignment",
            ),
            models.CheckConstraint(
                condition=(
                    Q(active=True, revoked_at__isnull=True, revoked_by__isnull=True)
                    | Q(active=False, revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="station_assignment_revocation_state",
            ),
        ]
