import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department
from apps.tablets.versions import validate_app_version


class Tablet(models.Model):
    class Status(models.TextChoices):
        INACTIVE = "INACTIVE", "Inactive"
        ACTIVE = "ACTIVE", "Active"
        LOST = "LOST", "Lost"
        RETIRED = "RETIRED", "Retired"

    # Physical asset states that participate in the normal operational lifecycle.
    OPERATIONAL_STATES = (Status.INACTIVE, Status.ACTIVE, Status.LOST)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="tablet_identities"
    )
    asset_number = models.CharField(max_length=128, blank=True)
    display_name = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INACTIVE)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_tablets",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=("INACTIVE", "ACTIVE", "LOST", "RETIRED")),
                name="tablet_status_is_current_asset_state",
            ),
            models.UniqueConstraint(
                fields=("department", "display_name"),
                name="tablet_display_name_unique_per_department",
            ),
            models.UniqueConstraint(
                fields=("department", "asset_number"),
                condition=~Q(asset_number=""),
                name="tablet_asset_number_unique_per_department",
            ),
        ]


class AppInstallation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        STALE = "STALE", "Stale"
        REVOKED = "REVOKED", "Revoked"
        REPLACED = "REPLACED", "Replaced"

    # Service-only result marker; it is deliberately not persisted.
    compatibility_blocked: bool = False

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tablet = models.ForeignKey(Tablet, on_delete=models.PROTECT, related_name="installations")
    installation_uuid = models.UUIDField(unique=True)
    credential_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    app_version = models.CharField(max_length=64, validators=[validate_app_version])
    adopted_app_version = models.CharField(max_length=64, validators=[validate_app_version])
    app_build = models.PositiveBigIntegerField(null=True, blank=True)
    app_version_seen_at = models.DateTimeField()
    hpke_public_key = models.BinaryField()
    hpke_ciphersuite = models.CharField(max_length=128)
    hpke_key_fingerprint = models.CharField(max_length=64)
    hpke_key_verified_at = models.DateTimeField()
    adopted_at = models.DateTimeField()
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="adopted_app_installations",
    )
    last_successful_check_in_at = models.DateTimeField(null=True, blank=True)
    authorization_valid_until = models.DateTimeField()
    stale_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=512, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tablet",),
                condition=Q(status__in=("ACTIVE", "STALE")),
                name="one_current_installation_per_tablet",
            )
        ]


class AdoptionInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tablet = models.ForeignKey(
        Tablet, on_delete=models.PROTECT, related_name="adoption_invitations"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_adoption_invitations",
    )
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    failed_attempt_count = models.PositiveSmallIntegerField(default=0)


class AdoptionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invitation = models.ForeignKey(
        AdoptionInvitation, on_delete=models.PROTECT, related_name="requests"
    )
    installation_uuid = models.UUIDField()
    app_version = models.CharField(max_length=64, validators=[validate_app_version])
    app_build = models.PositiveBigIntegerField(null=True, blank=True)
    hpke_public_key = models.BinaryField()
    hpke_public_key_fingerprint = models.CharField(max_length=64)
    hpke_ciphersuite = models.CharField(max_length=128)
    # The plaintext challenge nonce is deliberately never persisted.
    expected_hmac_digest = models.BinaryField()
    canonical_context_hash = models.CharField(max_length=64)
    encrypted_challenge = models.BinaryField()
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_replay_valid_until = models.DateTimeField(null=True, blank=True)
    completion_replay_invalidated_at = models.DateTimeField(null=True, blank=True)
    failed_attempt_count = models.PositiveSmallIntegerField(default=0)


class TabletApiActivity(models.Model):
    """Bounded, append-only diagnostic record of authenticated Tablet API requests.

    One row per resolved, authenticated installation request. Stores only safe
    metadata; never query strings, tokens, headers, bodies, or crypto material.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app_installation = models.ForeignKey(
        AppInstallation, on_delete=models.CASCADE, related_name="api_activity"
    )
    occurred_at = models.DateTimeField(db_index=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=256)
    status_code = models.PositiveSmallIntegerField()
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(
                fields=("app_installation", "-occurred_at"),
                name="tablet_api_act_install_idx",
            )
        ]
