import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department


class Tablet(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        STALE = "STALE", "Stale"
        REMOVED = "REMOVED", "Removed"
        LOST = "LOST", "Lost"
        RETIRED = "RETIRED", "Retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="tablet_identities"
    )
    asset_number = models.CharField(max_length=128, blank=True)
    display_name = models.CharField(max_length=255, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_tablets",
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="removed_tablets",
    )


class AppInstallation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        STALE = "STALE", "Stale"
        REVOKED = "REVOKED", "Revoked"
        REPLACED = "REPLACED", "Replaced"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tablet = models.ForeignKey(Tablet, on_delete=models.PROTECT, related_name="installations")
    installation_uuid = models.UUIDField(unique=True)
    credential_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    app_version = models.CharField(max_length=64)
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
    reactivated_at = models.DateTimeField(null=True, blank=True)
    reactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reactivated_app_installations",
    )
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


class ReactivationInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app_installation = models.ForeignKey(
        AppInstallation, on_delete=models.PROTECT, related_name="reactivation_invitations"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_reactivation_invitations",
    )
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    failed_attempt_count = models.PositiveSmallIntegerField(default=0)


class AdoptionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invitation = models.ForeignKey(
        AdoptionInvitation, null=True, blank=True, on_delete=models.PROTECT, related_name="requests"
    )
    reactivation_invitation = models.ForeignKey(
        ReactivationInvitation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    installation_uuid = models.UUIDField()
    app_version = models.CharField(max_length=64)
    hpke_public_key = models.BinaryField()
    hpke_public_key_fingerprint = models.CharField(max_length=64)
    hpke_ciphersuite = models.CharField(max_length=128)
    # The plaintext challenge nonce is deliberately never persisted.
    expected_hmac_digest = models.BinaryField()
    canonical_context_hash = models.CharField(max_length=64)
    encrypted_challenge = models.BinaryField()
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_attempt_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(invitation__isnull=False, reactivation_invitation__isnull=True)
                    | Q(invitation__isnull=True, reactivation_invitation__isnull=False)
                ),
                name="adoption_request_has_one_invitation",
            )
        ]
