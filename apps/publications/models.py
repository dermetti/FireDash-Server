import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department, Station
from apps.publications.paths import (
    document_artifact_relative_path,
    publication_artifact_relative_path,
)
from apps.publications.registry import DatasetRegistryError, validate_dataset_scope

MAX_CHANGE_SUMMARY_FIELDS = 20
MAX_CHANGE_SUMMARY_VALUE_LENGTH = 512


def validate_change_summary(summary: object) -> None:
    if not isinstance(summary, dict) or len(summary) > MAX_CHANGE_SUMMARY_FIELDS:
        raise ValidationError("Change summary exceeds the configured field limit.")
    if any(
        not isinstance(key, str)
        or len(key) > 128
        or len(str(value)) > MAX_CHANGE_SUMMARY_VALUE_LENGTH
        for key, value in summary.items()
    ):
        raise ValidationError("Change summary contains an invalid value.")


class DatasetScopeState(models.Model):
    class DeliveryFormat(models.TextChoices):
        V1_ARTIFACT = "V1_ARTIFACT", "V1 artifact"
        DOCUMENT_MANIFEST_V2 = "DOCUMENT_MANIFEST_V2", "Document manifest v2"

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
    delivery_format = models.CharField(
        max_length=32, choices=DeliveryFormat.choices, default=DeliveryFormat.V1_ARTIFACT
    )
    source_revision = models.PositiveBigIntegerField(default=0)
    # The current deterministic publishable canonical source. Empty means an
    # older scope has not yet been safely initialized by a locked service path.
    current_source_fingerprint = models.CharField(max_length=64, blank=True, default="")
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
        ]

    def clean(self) -> None:
        try:
            validate_dataset_scope(dataset_type_code=self.dataset_type_code, station=self.station)
        except DatasetRegistryError as error:
            raise ValidationError({"dataset_type_code": str(error)}) from error
        if self.station_id and self.station and self.station.department_id != self.department_id:
            raise ValidationError({"station": "Station must belong to the scope department."})


class DatasetPublication(models.Model):
    class ArtifactStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    class Status(models.TextChoices):
        STAGED = "STAGED", "Staged"
        BUILDING = "BUILDING", "Building"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        PUBLISHED = "PUBLISHED", "Published"
        FAILED = "FAILED", "Failed"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        REJECTED = "REJECTED", "Rejected"
        OBSOLETE = "OBSOLETE", "Obsolete"
        CANCELLED = "CANCELLED", "Cancelled"

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
    # Every staged build attempt receives one immutable scope-local version.
    # It is part of the artifact signature payload and is never reused.
    version_number = models.PositiveBigIntegerField()
    schema_version = models.PositiveIntegerField()
    source_revision = models.PositiveBigIntegerField()
    # The deterministic logical input to this immutable attempt.  This is
    # deliberately separate from artifact_sha256: encrypted artifact bytes can
    # change without any canonical dataset content changing.
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")
    # The canonical representation retained for source-aware lifecycle comparison
    # and a frozen build input. It contains logical distributed records/manifest
    # metadata, never ciphertext, PDF bytes, or signing material.
    # ``NULL`` intentionally means that the historical source snapshot is no
    # longer retained.  An empty object remains a valid snapshot for an empty
    # publishable source and must not be conflated with retention cleanup.
    source_snapshot = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BUILDING)
    build_summary = models.JSONField(default=dict)
    change_summary = models.JSONField(default=dict)
    artifact_ready = models.BooleanField(default=False)
    artifact_status = models.CharField(
        max_length=12, choices=ArtifactStatus.choices, default=ArtifactStatus.PENDING
    )
    artifact_path = models.CharField(max_length=512, blank=True)
    artifact_size = models.PositiveBigIntegerField(null=True, blank=True)
    artifact_sha256 = models.CharField(max_length=64, blank=True)
    artifact_nonce = models.BinaryField(null=True, blank=True)
    artifact_wrapped_cek = models.BinaryField(null=True, blank=True)
    artifact_encryption_algorithm = models.CharField(max_length=32, blank=True)
    artifact_wrapping_algorithm = models.CharField(max_length=32, blank=True)
    artifact_kek_version = models.CharField(max_length=64, blank=True)
    artifact_signature = models.BinaryField(null=True, blank=True)
    artifact_signature_algorithm = models.CharField(max_length=32, blank=True)
    artifact_signing_key_version = models.CharField(max_length=64, blank=True)
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
                # PostgreSQL treats department-scoped NULL station values as
                # equal here, so every assigned version is unique per scope.
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
                condition=~Q(status__in=("READY_FOR_REVIEW", "PUBLISHED"))
                | Q(artifact_status="READY"),
                name="review_publication_requires_ready_artifact",
            ),
        ]
        indexes = [
            # Bounded retention needs the newest usable successful attempts
            # per scope without scanning all publication history.
            models.Index(
                fields=("status", "scope_state", "-version_number"),
                name="pub_status_scope_ver_idx",
            ),
            # Terminal snapshot expiry is selected in small status/age batches.
            models.Index(fields=("status", "created_at"), name="pub_status_created_idx"),
        ]

    def clean(self) -> None:
        try:
            definition = validate_dataset_scope(
                dataset_type_code=self.dataset_type_code, station=self.station
            )
        except DatasetRegistryError as error:
            raise ValidationError({"dataset_type_code": str(error)}) from error
        errors = {}
        if self.station_id and self.station and self.station.department_id != self.department_id:
            errors["station"] = "Station must belong to the publication department."
        if self.schema_version != definition.current_schema_version:
            errors["schema_version"] = "Schema version is not supported for this dataset."
        if self.schema_version not in definition.supported_schema_versions:
            errors["schema_version"] = "Schema version is not supported for this dataset."
        if errors:
            raise ValidationError(errors)
        try:
            validate_change_summary(self.change_summary)
        except ValidationError as error:
            raise ValidationError({"change_summary": error.message}) from error
        if self.scope_state_id and (
            self.scope_state.department_id != self.department_id
            or self.scope_state.station_id != self.station_id
            or self.scope_state.dataset_type_code != self.dataset_type_code
        ):
            raise ValidationError({"scope_state": "Scope state must match the publication scope."})
        if self.artifact_path and self.artifact_path != publication_artifact_relative_path(
            department_id=self.department_id, publication_id=self.id
        ):
            raise ValidationError(
                {"artifact_path": "Artifact path must be a generated publication path."}
            )
        metadata_complete = all(
            (
                self.artifact_path,
                self.artifact_size is not None,
                len(self.artifact_sha256) == 64,
                self.artifact_nonce,
                self.artifact_wrapped_cek,
                self.artifact_encryption_algorithm == "AES-256-GCM",
                self.artifact_wrapping_algorithm == "AES-KW-RFC3394",
                self.artifact_kek_version,
                self.artifact_signature,
                self.artifact_signature_algorithm == "Ed25519",
                self.artifact_signing_key_version,
            )
        )
        if self.artifact_status == self.ArtifactStatus.READY and not metadata_complete:
            raise ValidationError("Ready artifacts require complete cryptographic metadata.")
        if self.status in (self.Status.READY_FOR_REVIEW, self.Status.PUBLISHED) and (
            self.artifact_status != self.ArtifactStatus.READY or not metadata_complete
        ):
            raise ValidationError(
                "Review-ready and published publications require a ready artifact."
            )


class FirePlanDocumentArtifact(models.Model):
    """Immutable, identity-addressed encrypted PDF for one canonical Fire Plan.

    This is intentionally independent of ``DatasetPublication``.  Reuse is
    scoped to its canonical source plan, never to equal bytes from another
    plan or department.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fire_plan = models.ForeignKey(
        "reference_data.FirePlan", on_delete=models.PROTECT, related_name="document_artifacts"
    )
    sanitized_pdf_sha256 = models.CharField(max_length=64)
    artifact_path = models.CharField(max_length=512, unique=True)
    ciphertext_size = models.PositiveBigIntegerField()
    ciphertext_sha256 = models.CharField(max_length=64)
    nonce = models.BinaryField()
    wrapped_cek = models.BinaryField()
    encryption_algorithm = models.CharField(max_length=32)
    wrapping_algorithm = models.CharField(max_length=32)
    kek_version = models.CharField(max_length=64)
    signature = models.BinaryField()
    signature_algorithm = models.CharField(max_length=32)
    signing_key_version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("fire_plan", "sanitized_pdf_sha256"),
                name="unique_fire_plan_document_artifact_content",
            ),
        ]
        indexes = [
            models.Index(fields=("fire_plan", "created_at"), name="doc_artifact_plan_created_idx")
        ]

    def clean(self) -> None:
        if self.artifact_path != document_artifact_relative_path(artifact_id=self.id):
            raise ValidationError(
                {"artifact_path": "Artifact path must be a generated document path."}
            )
        metadata_complete = all(
            (
                len(self.sanitized_pdf_sha256) == 64,
                self.ciphertext_size is not None,
                len(self.ciphertext_sha256) == 64,
                self.nonce and len(self.nonce) == 12,
                self.wrapped_cek,
                self.encryption_algorithm == "AES-256-GCM",
                self.wrapping_algorithm == "AES-KW-RFC3394",
                self.kek_version,
                self.signature,
                self.signature_algorithm == "Ed25519",
                self.signing_key_version,
            )
        )
        if not metadata_complete:
            raise ValidationError("Document artifacts require complete cryptographic metadata.")


class PublicationFirePlanArtifactReference(models.Model):
    """Frozen future-generation membership of a Fire Plan document artifact."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        DatasetPublication, on_delete=models.PROTECT, related_name="fire_plan_artifact_references"
    )
    fire_plan = models.ForeignKey(
        "reference_data.FirePlan",
        on_delete=models.PROTECT,
        related_name="publication_artifact_references",
    )
    document_artifact = models.ForeignKey(
        FirePlanDocumentArtifact,
        on_delete=models.PROTECT,
        related_name="publication_references",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "fire_plan"),
                name="unique_publication_fire_plan_artifact_reference",
            )
        ]

    def clean(self) -> None:
        errors = {}
        if self.document_artifact_id and self.document_artifact.fire_plan_id != self.fire_plan_id:
            errors["document_artifact"] = "Artifact must belong to the referenced Fire Plan."
        if (
            self.publication_id
            and self.fire_plan_id
            and self.publication.department_id != self.fire_plan.department_id
        ):
            errors["fire_plan"] = "Fire Plan must belong to the publication department."
        if errors:
            raise ValidationError(errors)


class FirePlanDocumentArtifactCleanup(models.Model):
    """Durable post-commit cleanup work for an unreachable document artifact."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artifact_id = models.UUIDField(unique=True)
    artifact_path = models.CharField(max_length=512, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        if self.artifact_path != document_artifact_relative_path(artifact_id=self.artifact_id):
            raise ValidationError(
                {"artifact_path": "Cleanup path must be a generated document path."}
            )


class FirePlanGenerationKey(models.Model):
    """Worker-held random key for one dormant Fire Plan v2 generation."""

    publication = models.OneToOneField(
        DatasetPublication,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="fire_plan_generation_key",
    )
    wrapped_key = models.BinaryField()
    wrapping_algorithm = models.CharField(max_length=32)
    kek_version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        if (
            self.wrapping_algorithm != "AES-KW-RFC3394"
            or not self.kek_version
            or not self.wrapped_key
        ):
            raise ValidationError("Generation keys require protected wrapping metadata.")


class FirePlanGenerationKeyGrant(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        DatasetPublication, on_delete=models.PROTECT, related_name="fire_plan_generation_key_grants"
    )
    app_installation = models.ForeignKey(
        "tablets.AppInstallation",
        on_delete=models.PROTECT,
        related_name="fire_plan_generation_key_grants",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    hpke_ciphersuite = models.CharField(max_length=128, blank=True)
    hpke_encapsulated_key = models.BinaryField(null=True, blank=True)
    hpke_wrapped_generation_key = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=512, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "app_installation"),
                name="unique_fire_plan_generation_key_grant",
            )
        ]


class FirePlanGenerationManifest(models.Model):
    """One deterministic signed complete manifest for a v2 generation."""

    publication = models.OneToOneField(
        DatasetPublication,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="fire_plan_generation_manifest",
    )
    payload = models.JSONField(default=dict)
    signature = models.BinaryField()
    signature_algorithm = models.CharField(max_length=32)
    signing_key_version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        if (
            self.signature_algorithm != "Ed25519"
            or not self.signing_key_version
            or not self.signature
        ):
            raise ValidationError("Generation manifests require a signature.")


class PublicationJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        OBSOLETE = "OBSOLETE", "Obsolete"
        CANCELLED = "CANCELLED", "Cancelled"

    class TriggerType(models.TextChoices):
        USER_REQUEST = "USER_REQUEST", "User request"
        BULK_REQUEST = "BULK_REQUEST", "Bulk request"
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
    not_before = models.DateTimeField(null=True, blank=True)
    debounce_started_at = models.DateTimeField(null=True, blank=True)
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


class DatasetKeyGrant(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        DatasetPublication, on_delete=models.PROTECT, related_name="key_grants"
    )
    app_installation = models.ForeignKey(
        "tablets.AppInstallation", on_delete=models.PROTECT, related_name="dataset_key_grants"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    hpke_ciphersuite = models.CharField(max_length=128, blank=True)
    hpke_encapsulated_key = models.BinaryField(null=True, blank=True)
    hpke_wrapped_content_key = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=512, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "app_installation"), name="unique_dataset_key_grant"
            )
        ]
        indexes = [
            models.Index(fields=("status", "created_at"), name="key_grant_status_created_idx")
        ]


class SignedManifest(models.Model):
    """Coalesced request and signed result for one installation state."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"
        OBSOLETE = "OBSOLETE", "Obsolete"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app_installation = models.ForeignKey(
        "tablets.AppInstallation", on_delete=models.PROTECT, related_name="signed_manifests"
    )
    state_hash = models.CharField(max_length=64)
    generation = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    payload = models.JSONField(default=dict)
    signature = models.BinaryField(null=True, blank=True)
    signature_algorithm = models.CharField(max_length=32, blank=True)
    signing_key_version = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=512, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("app_installation", "state_hash"), name="unique_signed_manifest_state"
            )
        ]
        indexes = [models.Index(fields=("status", "created_at"), name="signed_manifest_status_idx")]


class DatasetTypeRegistry(models.Model):
    """Database projection of the immutable application dataset registry."""

    code = models.CharField(primary_key=True, max_length=100)
    scope = models.CharField(max_length=16)
    current_schema_version = models.PositiveIntegerField()
    supported_schema_versions = models.JSONField(default=list)
    required = models.BooleanField(default=True)
    feature_code = models.CharField(max_length=100)


class DepartmentFeature(models.Model):
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="features")
    feature_code = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("department", "feature_code"), name="unique_department_feature"
            )
        ]
