"""Service-owned tablet provisioning and authorization lifecycle writes."""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    HPKEError,
    hpke_seal,
    parse_p256_public_key,
    public_key_fingerprint,
)
from apps.tablets.models import (
    AdoptionInvitation,
    AdoptionRequest,
    AppInstallation,
    ReactivationInvitation,
    Tablet,
)

AUTO_RENEW_THRESHOLD = timedelta(hours=48)
INVITATION_DURATION = timedelta(minutes=15)
CHALLENGE_DURATION = timedelta(minutes=5)
MAX_FAILED_ATTEMPTS = 5
ADOPTION_PROTOCOL = "tablet-adoption-v1"


class TabletError(ValueError):
    pass


def canonical_protocol_datetime(value: datetime) -> str:
    """Serialize an aware datetime in the canonical protocol UTC form.

    Adoption/reactivation ``expires_at`` is bound into the HPKE ``info`` and
    returned on the wire, so both must use the exact same string. DRF already
    renders UTC datetimes with a ``Z`` suffix, so the canonical form is UTC with
    ``Z`` (never ``+00:00``).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Protocol datetimes must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AdoptionChallengeContext:
    adoption_request_id: UUID
    installation_uuid: UUID
    tablet_id: UUID
    public_key_fingerprint: str
    expires_at: datetime
    mode: str

    def info(self) -> bytes:
        return _canonical_context(self).encode("ascii")


@dataclass(frozen=True)
class ProvisioningChallenge:
    request: AdoptionRequest
    encrypted_challenge: bytes


def _canonical_context(context: AdoptionChallengeContext) -> str:
    return json.dumps(
        {
            "adoption_request_id": str(context.adoption_request_id),
            "expires_at": canonical_protocol_datetime(context.expires_at),
            "hpke_ciphersuite": HPKE_CIPHERSUITE,
            "hpke_public_key_fingerprint": context.public_key_fingerprint,
            "installation_uuid": str(context.installation_uuid),
            "mode": context.mode,
            "protocol": ADOPTION_PROTOCOL,
            "tablet_id": str(context.tablet_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _secret_digest(value: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def generate_credential() -> str:
    """Create an opaque 256-bit installation credential for one-time delivery."""
    return secrets.token_urlsafe(32)


def verify_credential(*, installation: AppInstallation, credential: str) -> bool:
    return hmac.compare_digest(installation.credential_hash, _secret_digest(credential))


def _require_operational_tablet(tablet: Tablet) -> None:
    if tablet.department.status != tablet.department.Status.ACTIVE or not tablet.active:
        raise TabletError("Tablet department must be active.")
    if tablet.status in (Tablet.Status.REMOVED, Tablet.Status.LOST, Tablet.Status.RETIRED):
        raise TabletError("Tablet cannot be adopted or reactivated.")
    if not tablet.vehicle_assignments.filter(
        ended_at__isnull=True,
        valid_until__isnull=True,
        vehicle__active=True,
        vehicle__station__active=True,
    ).exists():
        raise TabletError("Tablet requires a current active vehicle assignment.")


def lease_target(*, department, now: datetime) -> datetime:
    """Return the department-owned maximum offline authorization lease target."""
    return now + timedelta(days=cast(int, department.tablet_lease_days))


def _eligible_for_lease_renewal(*, installation: AppInstallation, now) -> bool:
    return (
        installation.status == AppInstallation.Status.ACTIVE
        and installation.authorization_valid_until > now
        and installation.tablet.active
        and installation.tablet.status == Tablet.Status.ACTIVE
        and installation.tablet.department.status == installation.tablet.department.Status.ACTIVE
    )


def _renew_lease_if_due(*, installation: AppInstallation, now) -> bool:
    if not _eligible_for_lease_renewal(installation=installation, now=now):
        return False
    if installation.authorization_valid_until - now > AUTO_RENEW_THRESHOLD:
        return False
    installation.authorization_valid_until = lease_target(
        department=installation.tablet.department, now=now
    )
    return True


@transaction.atomic
def create_tablet(*, actor, department, display_name: str, asset_number: str = "") -> Tablet:
    require_department_admin(actor, department)
    if not display_name.strip():
        raise TabletError("Tablet display name is required.")
    tablet = Tablet.objects.create(
        department=department,
        display_name=display_name.strip(),
        asset_number=asset_number.strip(),
        created_by=actor,
    )
    record_event(
        action="tablet.created",
        actor_user=actor,
        department=department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@transaction.atomic
def create_adoption_invitation(
    *, actor, tablet: Tablet, expires_at=None
) -> tuple[AdoptionInvitation, str]:
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_operational_tablet(tablet)
    token = _new_token()
    invitation = AdoptionInvitation.objects.create(
        tablet=tablet,
        token_hash=_secret_digest(token),
        expires_at=expires_at or timezone.now() + INVITATION_DURATION,
        created_by=actor,
    )
    record_event(
        action="tablet.adoption_invitation_created",
        actor_user=actor,
        department=tablet.department,
        target_type="adoption_invitation",
        target_uuid=invitation.id,
    )
    return invitation, token


@transaction.atomic
def create_reactivation_invitation(
    *, actor, installation: AppInstallation, expires_at=None
) -> tuple[ReactivationInvitation, str]:
    installation = (
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .get(pk=installation.pk)
    )
    require_department_admin(actor, installation.tablet.department)
    _require_operational_tablet(installation.tablet)
    if installation.status != AppInstallation.Status.STALE:
        raise TabletError("Only stale installations can be reactivated.")
    token = _new_token()
    invitation = ReactivationInvitation.objects.create(
        app_installation=installation,
        token_hash=_secret_digest(token),
        expires_at=expires_at or timezone.now() + INVITATION_DURATION,
        created_by=actor,
    )
    record_event(
        action="tablet.reactivation_invitation_created",
        actor_user=actor,
        department=installation.tablet.department,
        target_type="reactivation_invitation",
        target_uuid=invitation.id,
    )
    return invitation, token


def _invitation_for_token(*, token: str, reactivation: bool):
    model = ReactivationInvitation if reactivation else AdoptionInvitation
    invitation = (
        model.objects.select_for_update()
        .select_related(
            "app_installation__tablet__department" if reactivation else "tablet__department"
        )
        .filter(token_hash=_secret_digest(token))
        .first()
    )
    if (
        invitation is None
        or invitation.used_at
        or invitation.revoked_at
        or invitation.expires_at <= timezone.now()
    ):
        raise TabletError("Invitation is invalid or expired.")
    return invitation


@transaction.atomic
def create_adoption_request(
    *,
    token: str,
    installation_uuid: UUID,
    app_version: str,
    hpke_public_key: bytes,
    hpke_ciphersuite: str,
    reactivation: bool = False,
) -> ProvisioningChallenge:
    if hpke_ciphersuite != HPKE_CIPHERSUITE:
        raise TabletError("Unsupported HPKE cipher suite.")
    invitation = _invitation_for_token(token=token, reactivation=reactivation)
    tablet = invitation.app_installation.tablet if reactivation else invitation.tablet
    _require_operational_tablet(tablet)
    if reactivation:
        installation = invitation.app_installation
        if (
            installation_uuid != installation.installation_uuid
            or hpke_ciphersuite != installation.hpke_ciphersuite
            or not hmac.compare_digest(bytes(installation.hpke_public_key), hpke_public_key)
        ):
            raise TabletError(
                "Installation identity or HPKE key does not match reactivation invitation."
            )
    try:
        public_key = parse_p256_public_key(hpke_public_key)
    except HPKEError as error:
        raise TabletError(str(error)) from error
    fingerprint = public_key_fingerprint(public_key)
    expires_at = timezone.now() + CHALLENGE_DURATION
    request = AdoptionRequest.objects.create(
        invitation=None if reactivation else invitation,
        reactivation_invitation=invitation if reactivation else None,
        installation_uuid=installation_uuid,
        app_version=app_version[:64],
        hpke_public_key=hpke_public_key,
        hpke_public_key_fingerprint=fingerprint,
        hpke_ciphersuite=hpke_ciphersuite,
        expected_hmac_digest=b"",
        canonical_context_hash="",
        encrypted_challenge=b"",
        expires_at=expires_at,
    )
    context = AdoptionChallengeContext(
        request.id,
        installation_uuid,
        tablet.id,
        fingerprint,
        expires_at,
        "reactivation" if reactivation else "adoption",
    )
    nonce = secrets.token_bytes(32)
    expected = hmac.digest(nonce, context.info(), "sha256")
    try:
        encapsulated_key, ciphertext = hpke_seal(
            plaintext=nonce, recipient_public_key=public_key, context=context
        )
    except HPKEError as error:
        raise TabletError(str(error)) from error
    request.expected_hmac_digest = expected
    request.canonical_context_hash = hashlib.sha256(context.info()).hexdigest()
    request.encrypted_challenge = encapsulated_key + ciphertext
    request.save(
        update_fields=("expected_hmac_digest", "canonical_context_hash", "encrypted_challenge")
    )
    return ProvisioningChallenge(
        request=request, encrypted_challenge=bytes(request.encrypted_challenge)
    )


@transaction.atomic
def _record_failed_attempt(*, request_id: UUID) -> None:
    now = timezone.now()
    request = (
        AdoptionRequest.objects.select_for_update(of=("self",))
        .select_related("invitation", "reactivation_invitation")
        .get(pk=request_id)
    )
    if request.completed_at:
        return
    if request.expires_at <= now:
        return
    AdoptionRequest.objects.filter(pk=request_id).update(
        failed_attempt_count=models.F("failed_attempt_count") + 1
    )
    request.refresh_from_db(fields=("failed_attempt_count",))
    invitation = request.reactivation_invitation or request.invitation
    if invitation is not None:
        inv_model = type(invitation)
        inv_model.objects.filter(pk=invitation.pk).update(
            failed_attempt_count=models.F("failed_attempt_count") + 1
        )
        invitation.refresh_from_db(fields=("failed_attempt_count",))
        if invitation.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
            inv_model.objects.filter(pk=invitation.pk).update(revoked_at=now)
    record_event(
        action="tablet.adoption_proof_failed",
        target_type="adoption_request",
        target_uuid=request.id,
        metadata={
            "failed_attempt_count": request.failed_attempt_count,
            "request_id": str(request.id),
        },
    )


@transaction.atomic
def _complete_successful_adoption(*, request_id: UUID) -> tuple[AppInstallation, str]:
    request = (
        AdoptionRequest.objects.select_for_update(of=("self",))
        .select_related(
            "invitation__tablet__department",
            "reactivation_invitation__app_installation__tablet__department",
        )
        .get(pk=request_id)
    )
    invitation = request.reactivation_invitation or request.invitation
    if invitation is not None:
        lock_model = type(invitation)
        invitation = lock_model.objects.select_for_update(of=("self",)).get(pk=invitation.pk)
    now = timezone.now()
    if request.completed_at:
        raise TabletError("Adoption request has already been completed.")
    if request.expires_at <= now:
        raise TabletError("Adoption request is expired.")
    if request.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
        raise TabletError("Adoption request has reached the maximum failed attempts.")
    if invitation is None:
        raise TabletError("Adoption request has no invitation.")
    if invitation.used_at:
        raise TabletError("Invitation has already been used.")
    if invitation.revoked_at:
        raise TabletError("Invitation has been revoked.")
    installation = (
        request.reactivation_invitation.app_installation
        if request.reactivation_invitation
        else None
    )
    if installation is not None:
        tablet = installation.tablet
    else:
        adoption_invitation = request.invitation
        if adoption_invitation is None:
            raise TabletError("Adoption request has no invitation.")
        tablet = adoption_invitation.tablet
    _require_operational_tablet(tablet)
    credential = generate_credential()
    if installation is None:
        AppInstallation.objects.filter(
            tablet=tablet, status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
        ).update(status=AppInstallation.Status.REPLACED)
        installation = AppInstallation.objects.create(
            tablet=tablet,
            installation_uuid=request.installation_uuid,
            credential_hash=_secret_digest(credential),
            status=AppInstallation.Status.ACTIVE,
            app_version=request.app_version,
            hpke_public_key=request.hpke_public_key,
            hpke_ciphersuite=request.hpke_ciphersuite,
            hpke_key_fingerprint=request.hpke_public_key_fingerprint,
            hpke_key_verified_at=now,
            adopted_at=now,
            adopted_by=invitation.created_by,
            authorization_valid_until=lease_target(department=tablet.department, now=now),
        )
        from apps.publications.manifests import revoke_dataset_key_grants

        for replaced_installation in AppInstallation.objects.filter(
            tablet=tablet, status=AppInstallation.Status.REPLACED
        ):
            revoke_dataset_key_grants(installation=replaced_installation)
        action = "tablet.adopted"
    else:
        if installation.status != AppInstallation.Status.STALE:
            raise TabletError("Only stale installations can be reactivated.")
        installation.credential_hash = _secret_digest(credential)
        installation.status = AppInstallation.Status.ACTIVE
        installation.authorization_valid_until = lease_target(department=tablet.department, now=now)
        installation.reactivated_at = now
        installation.reactivated_by = invitation.created_by
        installation.save(
            update_fields=(
                "credential_hash",
                "status",
                "authorization_valid_until",
                "reactivated_at",
                "reactivated_by",
            )
        )
        action = "tablet.reactivated"
    request.completed_at = now
    request.save(update_fields=("completed_at",))
    invitation.used_at = now
    invitation.save(update_fields=("used_at",))
    tablet.status = Tablet.Status.ACTIVE
    tablet.active = True
    tablet.save(update_fields=("status", "active"))
    record_event(
        action=action,
        actor_user=invitation.created_by,
        department=tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
        metadata={"hpke_key_fingerprint": installation.hpke_key_fingerprint},
    )
    return installation, credential


def complete_adoption(
    *, request_id: UUID, challenge_response: bytes, confirmed: bool, reactivation: bool = False
) -> tuple[AppInstallation, str]:
    if not confirmed:
        raise TabletError("Adoption was not confirmed.")
    request = AdoptionRequest.objects.select_related(
        "invitation__tablet__department",
        "reactivation_invitation__app_installation__tablet__department",
    ).get(pk=request_id)
    invitation = request.reactivation_invitation or request.invitation
    if (request.reactivation_invitation is not None) != reactivation:
        raise TabletError("Adoption request mode does not match the completion endpoint.")
    if invitation is None:
        raise TabletError("Adoption request has no invitation.")
    now = timezone.now()
    if request.completed_at:
        raise TabletError("Adoption request has already been completed.")
    if request.expires_at <= now:
        raise TabletError("Adoption request is expired.")
    if request.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
        raise TabletError("Adoption request has reached the maximum failed attempts.")
    if invitation is None:
        raise TabletError("Adoption request has no invitation.")
    if invitation.used_at:
        raise TabletError("Invitation has already been used.")
    if invitation.revoked_at:
        raise TabletError("Invitation has been revoked.")
    proof_valid = hmac.compare_digest(bytes(request.expected_hmac_digest), challenge_response)
    if not proof_valid:
        _record_failed_attempt(request_id=request_id)
        raise TabletError("Adoption proof is invalid.")
    return _complete_successful_adoption(request_id=request_id)


@transaction.atomic
def check_in(*, installation: AppInstallation, credential: str) -> AppInstallation:
    installation = (
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .get(pk=installation.pk)
    )
    now = timezone.now()
    if not verify_credential(installation=installation, credential=credential):
        raise PermissionDenied("Installation credential is invalid.")
    if (
        installation.status != AppInstallation.Status.ACTIVE
        or installation.authorization_valid_until <= now
    ):
        if installation.status == AppInstallation.Status.ACTIVE:
            _mark_stale(installation, now)
        raise TabletError("Installation is not active.")
    _require_operational_tablet(installation.tablet)
    installation.last_successful_check_in_at = now
    if _renew_lease_if_due(installation=installation, now=now):
        installation.save(
            update_fields=("last_successful_check_in_at", "authorization_valid_until")
        )
    else:
        installation.save(update_fields=("last_successful_check_in_at",))
    return installation


@transaction.atomic
def refresh_installation_lease(
    *, installation: AppInstallation, credential: str
) -> AppInstallation:
    """Explicitly top up one eligible installation without creating delivery work."""
    installation = (
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .get(pk=installation.pk)
    )
    now = timezone.now()
    if not verify_credential(installation=installation, credential=credential):
        raise PermissionDenied("Installation credential is invalid.")
    if (
        installation.status != AppInstallation.Status.ACTIVE
        or installation.authorization_valid_until <= now
    ):
        raise TabletError("Installation is not active.")
    _require_operational_tablet(installation.tablet)
    if not _eligible_for_lease_renewal(installation=installation, now=now):
        raise TabletError("Only an active, authorized installation can be renewed.")
    old_expiry = installation.authorization_valid_until
    target = lease_target(department=installation.tablet.department, now=now)
    installation.authorization_valid_until = max(old_expiry, target)
    installation.last_successful_check_in_at = now
    installation.save(update_fields=("authorization_valid_until", "last_successful_check_in_at"))
    record_event(
        action="tablet.self_refreshed",
        department=installation.tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
        metadata={
            "old_expiry": old_expiry.isoformat(),
            "new_expiry": installation.authorization_valid_until.isoformat(),
        },
    )
    return installation


def _mark_stale(installation: AppInstallation, now) -> None:
    installation.status = AppInstallation.Status.STALE
    installation.stale_at = now
    installation.save(update_fields=("status", "stale_at"))
    Tablet.objects.filter(pk=installation.tablet_id, status=Tablet.Status.ACTIVE).update(
        status=Tablet.Status.STALE
    )
    record_event(
        action="tablet.became_stale",
        department=installation.tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
    )


@transaction.atomic
def mark_stale_installations(*, now=None) -> int:
    now = now or timezone.now()
    installations = list(
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .filter(status=AppInstallation.Status.ACTIVE, authorization_valid_until__lte=now)
    )
    for installation in installations:
        _mark_stale(installation, now)
    return len(installations)


@transaction.atomic
def remove_tablet(*, actor, tablet: Tablet, status: str, reason: str) -> Tablet:
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    if status not in (Tablet.Status.REMOVED, Tablet.Status.LOST, Tablet.Status.RETIRED):
        raise TabletError("Tablet removal status is invalid.")
    now = timezone.now()
    tablet.status, tablet.active, tablet.removed_at, tablet.removed_by = status, False, now, actor
    tablet.save(update_fields=("status", "active", "removed_at", "removed_by"))
    AppInstallation.objects.filter(
        tablet=tablet, status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
    ).update(status=AppInstallation.Status.REVOKED, revoked_at=now, revocation_reason=reason[:512])
    from apps.publications.manifests import revoke_dataset_key_grants

    revoked_installations = AppInstallation.objects.filter(
        tablet=tablet, status=AppInstallation.Status.REVOKED
    )
    for installation in revoked_installations:
        revoke_dataset_key_grants(installation=installation)
    AdoptionInvitation.objects.filter(
        tablet=tablet, used_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now)
    ReactivationInvitation.objects.filter(
        app_installation__tablet=tablet, used_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now)
    record_event(
        action="tablet.removed",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
        metadata={"status": status},
    )
    return tablet
