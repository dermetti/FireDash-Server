import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department, Station
from apps.publications.registry import DatasetRegistryError, validate_dataset_scope


class DatasetScopeState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="dataset_scopes"
    )
    station = models.ForeignKey(
        Station,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dataset_scopes",
    )
    dataset_type_code = models.CharField(max_length=100)
    source_revision = models.PositiveBigIntegerField(default=0)
    dirty_since = models.DateTimeField(null=True, blank=True)
    latest_built_publication = models.ForeignKey(
        "DatasetPublication",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="latest_for_scopes",
    )
    current_published_publication = models.ForeignKey(
        "DatasetPublication",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_for_scopes",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code"),
                nulls_distinct=False,
                name="unique_dataset_scope_state",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        dataset_type_code__in=(
                            "department_hydrants",
                            "department_fire_plans",
                        ),
                        station__isnull=True,
                    )
                    | Q(dataset_type_code="station_personnel", station__isnull=False)
                ),
                name="registered_dataset_scope_state_scope",
            ),
        ]

    def clean(self) -> None:
        try:
            validate_dataset_scope(dataset_type_code=self.dataset_type_code, station=self.station)
        except DatasetRegistryError as error:
            raise ValidationError({"dataset_type_code": str(error)}) from error
        if self.station_id and self.station and self.station.department_id != self.department_id:
            raise ValidationError({"station": "Station must belong to the scope department."})


class DatasetPublication(models.Model):
    class Status(models.TextChoices):
        BUILDING = "BUILDING", "Building"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        PUBLISHED = "PUBLISHED", "Published"
        FAILED = "FAILED", "Failed"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        REJECTED = "REJECTED", "Rejected"
        OBSOLETE = "OBSOLETE", "Obsolete"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="dataset_publications"
    )
    station = models.ForeignKey(
        Station,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dataset_publications",
    )
    dataset_type_code = models.CharField(max_length=100)
    scope_state = models.ForeignKey(
        DatasetScopeState, on_delete=models.PROTECT, related_name="publications"
    )
    version_number = models.PositiveBigIntegerField()
    schema_version = models.PositiveIntegerField()
    source_revision = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BUILDING)
    build_summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_dataset_publications",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_dataset_publications",
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    build_error = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code", "version_number"),
                nulls_distinct=False,
                name="unique_dataset_publication_version",
            ),
            models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code"),
                condition=Q(status="PUBLISHED"),
                nulls_distinct=False,
                name="one_current_published_dataset_publication",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        dataset_type_code__in=(
                            "department_hydrants",
                            "department_fire_plans",
                        ),
                        station__isnull=True,
                    )
                    | Q(dataset_type_code="station_personnel", station__isnull=False)
                ),
                name="registered_dataset_publication_scope",
            ),
        ]

    def clean(self) -> None:
        try:
            definition = validate_dataset_scope(
                dataset_type_code=self.dataset_type_code, station=self.station
            )
        except DatasetRegistryError as error:
            raise ValidationError({"dataset_type_code": str(error)}) from error
        if self.station_id and self.station and self.station.department_id != self.department_id:
            raise ValidationError({"station": "Station must belong to the publication department."})
        if self.schema_version != definition.current_schema_version:
            raise ValidationError(
                {"schema_version": "Schema version is not current for this dataset."}
            )
        if self.scope_state_id and (
            self.scope_state.department_id != self.department_id
            or self.scope_state.station_id != self.station_id
            or self.scope_state.dataset_type_code != self.dataset_type_code
        ):
            raise ValidationError({"scope_state": "Scope state must match the publication scope."})


class PublicationJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        OBSOLETE = "OBSOLETE", "Obsolete"

    class TriggerType(models.TextChoices):
        USER_REQUEST = "USER_REQUEST", "User request"
        DATA_CHANGE = "DATA_CHANGE", "Data change"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset_type_code = models.CharField(max_length=100)
    scope_state = models.ForeignKey(
        DatasetScopeState, on_delete=models.PROTECT, related_name="publication_jobs"
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="publication_jobs"
    )
    station = models.ForeignKey(
        Station, null=True, blank=True, on_delete=models.PROTECT, related_name="publication_jobs"
    )
    source_revision = models.PositiveBigIntegerField()
    attempt_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_publication_jobs",
    )
    trigger_type = models.CharField(max_length=16, choices=TriggerType.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=2000, blank=True)
    error_category = models.CharField(max_length=32, blank=True)
    build_publication = models.ForeignKey(
        DatasetPublication,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="build_jobs",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code"),
                condition=Q(status__in=("PENDING", "RUNNING")),
                nulls_distinct=False,
                name="one_active_publication_job_per_scope",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        dataset_type_code__in=(
                            "department_hydrants",
                            "department_fire_plans",
                        ),
                        station__isnull=True,
                    )
                    | Q(dataset_type_code="station_personnel", station__isnull=False)
                ),
                name="registered_publication_job_scope",
            ),
        ]
        indexes = [models.Index(fields=("status", "created_at"), name="pub_job_status_created_idx")]

    def clean(self) -> None:
        try:
            validate_dataset_scope(dataset_type_code=self.dataset_type_code, station=self.station)
        except DatasetRegistryError as error:
            raise ValidationError({"dataset_type_code": str(error)}) from error
        if self.station_id and self.station and self.station.department_id != self.department_id:
            raise ValidationError({"station": "Station must belong to the job department."})
        if self.scope_state_id and (
            self.scope_state.department_id != self.department_id
            or self.scope_state.station_id != self.station_id
            or self.scope_state.dataset_type_code != self.dataset_type_code
        ):
            raise ValidationError({"scope_state": "Scope state must match the job scope."})


class PublicationActivation(models.Model):
    class Action(models.TextChoices):
        PUBLISH = "PUBLISH", "Publish"
        ROLLBACK = "ROLLBACK", "Rollback"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        DatasetPublication, on_delete=models.PROTECT, related_name="activations"
    )
    scope_state = models.ForeignKey(
        DatasetScopeState, on_delete=models.PROTECT, related_name="publication_activations"
    )
    previous_publication = models.ForeignKey(
        DatasetPublication,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replaced_by_activations",
    )
    action = models.CharField(max_length=12, choices=Action.choices)
    activated_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="publication_activations",
    )
