"""Stable source identities; immutable content is independent of the source tree."""

import uuid
from datetime import timedelta
from typing import cast

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from apps.organizations.models import Station


def default_quota():
    return settings.FILE_LIBRARY_QUOTA_BYTES


def default_department_quota() -> int:
    return settings.FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES


def default_station_quota() -> int:
    return settings.FILE_LIBRARY_STATION_QUOTA_BYTES


class DepartmentQuotaPolicy(models.Model):
    department = models.OneToOneField("organizations.Department", on_delete=models.PROTECT)
    department_files_bytes = models.PositiveBigIntegerField(default=default_department_quota)
    station_files_bytes = models.PositiveBigIntegerField(default=default_station_quota)


class Library(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.OneToOneField(
        "organizations.Department", null=True, on_delete=models.PROTECT
    )
    station = models.OneToOneField("organizations.Station", null=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=255, default="Documents")
    source_revision = models.PositiveBigIntegerField(default=0)
    usage_bytes = models.PositiveBigIntegerField(default=0)

    @property
    def quota_bytes(self):
        department_id = self.department_id or cast(Station, self.station).department_id
        policy, _ = DepartmentQuotaPolicy.objects.get_or_create(department_id=department_id)
        return policy.department_files_bytes if self.department_id else policy.station_files_bytes

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(department__isnull=False, station__isnull=True)
                    | Q(department__isnull=True, station__isnull=False)
                ),
                name="file_library_exact_scope",
            ),
        ]


class Content(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    library = models.ForeignKey(Library, on_delete=models.PROTECT)
    storage_key = models.CharField(max_length=255, unique=True)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class PublicationContent(models.Model):
    """Pins frozen source bytes from worker claim until publication retirement."""

    publication = models.ForeignKey("publications.DatasetPublication", on_delete=models.PROTECT)
    content = models.ForeignKey(Content, on_delete=models.PROTECT, related_name="publications")
    document_id = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "document_id"), name="library_publication_document"
            )
        ]


class TemporaryStationAccess(models.Model):
    """Time-bounded, user-bound department-admin access to one station library."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="temporary_station_file_accesses",
    )
    department = models.ForeignKey("organizations.Department", on_delete=models.PROTECT)
    station = models.ForeignKey("organizations.Station", on_delete=models.PROTECT)
    activated_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(revoked_at__isnull=True),
                name="one_active_temporary_station_access",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("activated_at")),
                name="temporary_station_access_positive_duration",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__lte=models.F("activated_at") + timedelta(hours=6)),
                name="temporary_station_access_six_hour_max",
            ),
            models.CheckConstraint(
                condition=(
                    Q(revoked_at__isnull=True, revocation_reason="")
                    | Q(revoked_at__isnull=False) & ~Q(revocation_reason="")
                ),
                name="temporary_station_access_revocation_state",
            ),
        ]


class Entry(models.Model):
    """One namespace for folders and logical PDF documents, including at root."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    library = models.ForeignKey(Library, on_delete=models.PROTECT, related_name="entries")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=255)
    is_folder = models.BooleanField(default=False)
    content = models.ForeignKey(
        Content, null=True, on_delete=models.PROTECT, related_name="documents"
    )
    modified_at = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        ordering = ("-is_folder", "name", "id")
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "library",
                "parent",
                nulls_distinct=False,
                name="file_entry_unique_name",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_folder=True, content__isnull=True)
                    | Q(is_folder=False, content__isnull=False)
                ),
                name="file_entry_content_kind",
            ),
        ]


class StorageImpact(models.Model):
    """Source-side capacity reservations, never delivery receipts."""

    library = models.ForeignKey(Library, on_delete=models.PROTECT)
    document_id = models.UUIDField()
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class CapacityReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    library = models.ForeignKey(Library, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="capacity_requests",
    )
    operation = models.JSONField()
    projection = models.JSONField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True)
    decision = models.CharField(max_length=12, default="pending")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="capacity_decisions",
    )
    decided_at = models.DateTimeField(null=True)
    consumed_at = models.DateTimeField(null=True)
    # NULL identifies historical upload reviews, which never authorize publication.
    publication_snapshot = models.JSONField(null=True)
    superseded_at = models.DateTimeField(null=True)
    publication = models.ForeignKey(
        "publications.DatasetPublication",
        null=True,
        on_delete=models.PROTECT,
        related_name="capacity_reviews",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(decision="pending", decided_by__isnull=True, decided_at__isnull=True)
                    | Q(
                        decision__in=("approved", "rejected"),
                        decided_by__isnull=False,
                        decided_at__isnull=False,
                    )
                ),
                name="capacity_review_decision_state",
            ),
            models.CheckConstraint(
                condition=Q(consumed_at__isnull=True) | Q(decision="approved"),
                name="capacity_review_consumed_approved",
            ),
        ]
