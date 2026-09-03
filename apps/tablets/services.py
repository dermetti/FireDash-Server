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
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
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
    Tablet,
)
from apps.tablets.versions import AppVersionError, parse_app_build, parse_app_version

AUTO_RENEW_THRESHOLD = timedelta(hours=48)
INVITATION_DURATION = timedelta(minutes=15)
CHALLENGE_DURATION = timedelta(minutes=5)
MAX_FAILED_ATTEMPTS = 5
ADOPTION_PROTOCOL = "tablet-adoption-v1"
COMPLETION_REPLAY_DURATION = timedelta(minutes=10)
MAX_ASSET_NUMBER_ALLOCATION_ATTEMPTS = 1000

# Authoritative physical asset lifecycle. Each intent-driven service enforces
# these transitions server-side; the UI only surfaces what is valid.
#   INACTIVE → ACTIVE, LOST, RETIRED
#   ACTIVE   → INACTIVE, LOST, RETIRED
#   LOST     → INACTIVE
#   RETIRED  → (terminal)
ASSET_TRANSITIONS: dict[str, tuple[str, ...]] = {
    Tablet.Status.INACTIVE: (Tablet.Status.ACTIVE, Tablet.Status.LOST, Tablet.Status.RETIRED),
    Tablet.Status.ACTIVE: (Tablet.Status.INACTIVE, Tablet.Status.LOST, Tablet.Status.RETIRED),
    Tablet.Status.LOST: (Tablet.Status.INACTIVE,),
}


class TabletError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def canonical_protocol_datetime(value: datetime) -> str:
    """Serialize an aware datetime in the canonical protocol UTC form.

    Adoption ``expires_at`` is bound into the HPKE ``info`` and
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


def _has_current_operational_assignment(*, tablet: Tablet, now: datetime) -> bool:
    return (
        tablet.vehicle_assignments.filter(
            valid_from__lte=now,
            ended_at__isnull=True,
            vehicle__active=True,
            vehicle__station__active=True,
        )
        .filter(
            models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=now),
            vehicle__department_id=tablet.department_id,
        )
        .exists()
    )


def _require_operational_tablet(tablet: Tablet, *, now: datetime | None = None) -> None:
    """Require prerequisites for adoption, which is allowed while the asset is inactive."""
    now = now or timezone.now()
    if tablet.department.status != tablet.department.Status.ACTIVE or not tablet.active:
        raise TabletError("Tablet department must be active.", code="installation_inactive")
    if tablet.status not in (Tablet.Status.INACTIVE, Tablet.Status.ACTIVE):
        raise TabletError("Tablet cannot be adopted.", code="installation_inactive")
    if not _has_current_operational_assignment(tablet=tablet, now=now):
        raise TabletError(
            "Tablet requires a current active vehicle assignment.", code="installation_inactive"
        )


def _require_active_operational_tablet(*, tablet: Tablet, now: datetime) -> None:
    """Require the asset state and scope needed for operational authorization."""
    if tablet.department.status != tablet.department.Status.ACTIVE or not tablet.active:
        raise TabletError("Tablet department must be active.", code="installation_inactive")
    if tablet.status != Tablet.Status.ACTIVE:
        raise TabletError(
            "Tablet is not active for operational service.", code="installation_inactive"
        )
    if not _has_current_operational_assignment(tablet=tablet, now=now):
        raise TabletError(
            "Tablet requires a current active vehicle assignment.", code="installation_inactive"
        )


def _require_inactive_control_tablet(*, tablet: Tablet) -> None:
    """Allow a commissioned-but-inactive asset to synchronize control state only."""
    if tablet.department.status != tablet.department.Status.ACTIVE or not tablet.active:
        raise TabletError("Tablet department must be active.", code="installation_inactive")
    if tablet.status != Tablet.Status.INACTIVE:
        raise TabletError(
            "Tablet is not available for control-plane synchronization.",
            code="installation_inactive",
        )


def update_app_telemetry(
    *,
    installation: AppInstallation,
    app_version: str | None,
    app_build: int | None,
    build_supplied: bool,
    now: datetime,
) -> bool:
    """Update telemetry only when a complete, valid version was supplied."""
    if app_version is None:
        return False
    if app_version == installation.app_version:
        installation.app_version_seen_at = now
        if build_supplied:
            installation.app_build = app_build
    else:
        installation.app_version = app_version
        installation.app_build = app_build if build_supplied else None
        installation.app_version_seen_at = now
    return True


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


def _is_current_installation(*, installation: AppInstallation) -> bool:
    return (
        not AppInstallation.objects.filter(
            tablet_id=installation.tablet_id,
            status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE),
        )
        .exclude(pk=installation.pk)
        .exists()
    )


def _recover_stale_installation(*, installation: AppInstallation, now: datetime) -> None:
    """Restore a still-current stale installation after durable-credential proof.

    A stale lease is an availability condition, not credential revocation. This
    deliberately leaves REVOKED and REPLACED credentials outside the recovery
    path, and requires the physical asset to be actively commissioned again.
    """
    if installation.status != AppInstallation.Status.STALE or not _is_current_installation(
        installation=installation
    ):
        raise TabletError("Installation is not active.", code="installation_inactive")
    _require_active_operational_tablet(tablet=installation.tablet, now=now)
    installation.status = AppInstallation.Status.ACTIVE
    installation.authorization_valid_until = lease_target(
        department=installation.tablet.department, now=now
    )
    installation.last_successful_check_in_at = now
    installation.save(
        update_fields=("status", "authorization_valid_until", "last_successful_check_in_at")
    )
    record_event(
        action="tablet.stale_auto_recovered",
        department=installation.tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
    )


def _formatted_asset_number(*, department: Department, sequence: int) -> str:
    numeric_part = str(sequence).zfill(department.tablet_asset_number_width)
    asset_number = f"{department.tablet_asset_number_prefix}{numeric_part}"
    max_length = Tablet._meta.get_field("asset_number").max_length
    if len(asset_number) > max_length:
        raise TabletError(
            "The next generated asset number does not fit the Tablet asset-number length.",
            code="asset_number_exhausted",
        )
    return asset_number


def tablet_asset_number_preview(*, department: Department) -> str:
    """Return the unreserved next formatting candidate for registration UI.

    This intentionally reads no lock and changes no sequence.  A POST always
    re-evaluates and allocates under the authoritative Department row lock, so
    a preview can never imply a reservation.
    """
    return _formatted_asset_number(
        department=department, sequence=department.tablet_asset_number_sequence + 1
    )


def _next_generated_asset_number(*, department: Department) -> str:
    """Advance the locked Department-local sequence and return its formatted value.

    ``create_tablet`` always locks the Department row before calling this helper.
    That single lock serializes automatic allocation, policy updates, and normal
    manual creation inside FireDash.  The caller's outer transaction couples the
    sequence advancement to the Tablet creation, so failed registrations roll
    both changes back.
    """
    sequence = department.tablet_asset_number_sequence + 1
    asset_number = _formatted_asset_number(department=department, sequence=sequence)
    department.tablet_asset_number_sequence = sequence
    department.save(update_fields=("tablet_asset_number_sequence",))
    return asset_number


def _create_generated_tablet(*, actor, department: Department, display_name: str) -> Tablet:
    """Create one Tablet using the locked Department's persistent allocator.

    Manual identifiers can legally look like generated identifiers.  Existing
    values are skipped before attempting the insert.  The nested savepoint also
    handles a direct/out-of-band insert racing this service without poisoning the
    enclosing transaction.  The loop is deliberately bounded.
    """
    for _ in range(MAX_ASSET_NUMBER_ALLOCATION_ATTEMPTS):
        asset_number = _next_generated_asset_number(department=department)
        if Tablet.objects.filter(department=department, asset_number=asset_number).exists():
            continue
        try:
            with transaction.atomic():
                return Tablet.objects.create(
                    department=department,
                    display_name=display_name,
                    asset_number=asset_number,
                    created_by=actor,
                )
        except IntegrityError as error:
            # A concurrent direct write may have claimed the generated identifier
            # without following the Department-row locking convention.  Treat only
            # that collision as retryable; other integrity failures remain errors.
            if Tablet.objects.filter(department=department, asset_number=asset_number).exists():
                continue
            raise TabletError(
                "A tablet with this display name already exists in the department."
            ) from error
    raise TabletError(
        "Unable to allocate a unique Tablet asset number after "
        f"{MAX_ASSET_NUMBER_ALLOCATION_ATTEMPTS} attempts.",
        code="asset_number_allocation_exhausted",
    )


@transaction.atomic
def create_tablet(
    *,
    actor,
    department: Department,
    display_name: str,
    asset_number: str = "",
    generate_asset_number: bool | None = None,
) -> Tablet:
    # Department is the allocator's authoritative row and is deliberately
    # acquired first.  This lock ordering is shared by policy updates and all
    # normal Tablet registration paths.
    department = Department.objects.select_for_update().get(pk=department.pk)
    require_department_admin(actor, department)
    if not display_name.strip():
        raise TabletError("Tablet display name is required.")
    display_name = display_name.strip()
    asset_number = asset_number.strip()
    if Tablet.objects.filter(department=department, display_name=display_name).exists():
        raise TabletError("A tablet with this display name already exists in the department.")
    # The Department policy is the sole automatic-numbering decision.  The
    # retained argument is deliberately ignored for source compatibility with
    # callers from the earlier per-registration UI; it cannot override policy.
    if department.tablet_asset_number_auto_enabled:
        tablet = _create_generated_tablet(
            actor=actor, department=department, display_name=display_name
        )
    else:
        if (
            asset_number
            and Tablet.objects.filter(department=department, asset_number=asset_number).exists()
        ):
            raise TabletError("A tablet with this asset number already exists in the department.")
        try:
            tablet = Tablet.objects.create(
                department=department,
                display_name=display_name,
                asset_number=asset_number,
                created_by=actor,
            )
        except IntegrityError as error:
            raise TabletError(
                "A tablet with this display name or asset number already exists in the department."
            ) from error
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
def initiate_installation_replacement(
    *, actor, tablet: Tablet, expires_at=None
) -> tuple[AdoptionInvitation, str]:
    """Start the administrative Re-provision FireDash workflow.

    This is a thin wrapper over the existing hardened adoption lifecycle. It
    issues a fresh adoption invitation while the current installation remains
    authoritative; only a successful new adoption transitions the previous
    installation to ``REPLACED`` (with grant revocation and purge semantics) in
    ``_complete_successful_adoption``.
    """
    invitation, token = create_adoption_invitation(
        actor=actor, tablet=tablet, expires_at=expires_at
    )
    record_event(
        action="tablet.installation_replacement_initiated",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
        metadata={"adoption_invitation_id": str(invitation.id)},
    )
    return invitation, token


def _invitation_for_token(*, token: str) -> AdoptionInvitation:
    invitation = (
        AdoptionInvitation.objects.select_for_update()
        .select_related("tablet__department")
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
    app_build: int | None = None,
    hpke_public_key: bytes,
    hpke_ciphersuite: str,
) -> ProvisioningChallenge:
    try:
        app_version = str(parse_app_version(app_version))
        if app_build is not None:
            app_build = parse_app_build(app_build)
    except AppVersionError as error:
        raise TabletError(str(error), code="invalid_request") from error
    if hpke_ciphersuite != HPKE_CIPHERSUITE:
        raise TabletError("Unsupported HPKE cipher suite.")
    invitation = _invitation_for_token(token=token)
    tablet = invitation.tablet
    _require_operational_tablet(tablet)
    try:
        public_key = parse_p256_public_key(hpke_public_key)
    except HPKEError as error:
        raise TabletError(str(error)) from error
    fingerprint = public_key_fingerprint(public_key)
    expires_at = timezone.now() + CHALLENGE_DURATION
    request = AdoptionRequest.objects.create(
        invitation=invitation,
        installation_uuid=installation_uuid,
        app_version=app_version[:64],
        app_build=app_build,
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
        "adoption",
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
        .select_related("invitation")
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
    invitation = request.invitation
    if invitation is not None:
        AdoptionInvitation.objects.filter(pk=invitation.pk).update(
            failed_attempt_count=models.F("failed_attempt_count") + 1
        )
        invitation.refresh_from_db(fields=("failed_attempt_count",))
        if invitation.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
            AdoptionInvitation.objects.filter(pk=invitation.pk).update(revoked_at=now)
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
        .select_related("invitation__tablet__department")
        .get(pk=request_id)
    )
    invitation = request.invitation
    if invitation is not None:
        invitation = AdoptionInvitation.objects.select_for_update(of=("self",)).get(
            pk=invitation.pk
        )
    now = timezone.now()
    if request.completed_at:
        raise TabletError(
            "Adoption request has already been completed.", code="adoption_request_completed"
        )
    if request.expires_at <= now:
        raise TabletError("Adoption request is expired.", code="adoption_request_expired")
    if request.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
        raise TabletError(
            "Adoption request has reached the maximum failed attempts.",
            code="adoption_attempt_limit_reached",
        )
    if invitation is None:
        raise TabletError("Adoption request has no invitation.", code="invalid_request")
    if invitation.used_at:
        raise TabletError("Invitation has already been used.", code="invitation_invalid")
    if invitation.revoked_at:
        raise TabletError("Invitation has been revoked.", code="invitation_invalid")
    tablet = invitation.tablet
    _require_operational_tablet(tablet)
    # A client that lost its locally persisted credential must replay its
    # original completed request.  It must not be able to use a new invitation
    # and preview to create another installation with an already-adopted local
    # identity.  Apart from being outside the recovery contract, that path used
    # to fall through to AppInstallation's unique installation_uuid constraint
    # and escape as an HTTP 500.
    if AppInstallation.objects.filter(installation_uuid=request.installation_uuid).exists():
        raise TabletError(
            "Installation UUID is already provisioned; replay the original completion request.",
            code="invalid_request",
        )
    credential = generate_credential()
    AppInstallation.objects.filter(
        tablet=tablet, status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
    ).update(status=AppInstallation.Status.REPLACED)
    try:
        # Keep an inner savepoint so a concurrent direct writer which claims
        # this UUID can be translated without leaving the outer completion
        # transaction unusable.
        with transaction.atomic():
            installation = AppInstallation.objects.create(
                tablet=tablet,
                installation_uuid=request.installation_uuid,
                credential_hash=_secret_digest(credential),
                status=AppInstallation.Status.ACTIVE,
                app_version=request.app_version,
                adopted_app_version=request.app_version,
                app_build=request.app_build,
                app_version_seen_at=now,
                hpke_public_key=request.hpke_public_key,
                hpke_ciphersuite=request.hpke_ciphersuite,
                hpke_key_fingerprint=request.hpke_public_key_fingerprint,
                hpke_key_verified_at=now,
                adopted_at=now,
                adopted_by=invitation.created_by,
                authorization_valid_until=lease_target(department=tablet.department, now=now),
            )
    except IntegrityError as error:
        if AppInstallation.objects.filter(installation_uuid=request.installation_uuid).exists():
            raise TabletError(
                "Installation UUID is already provisioned; replay the original completion request.",
                code="invalid_request",
            ) from error
        raise
    from apps.publications.manifests import revoke_dataset_key_grants

    for replaced_installation in AppInstallation.objects.filter(
        tablet=tablet, status=AppInstallation.Status.REPLACED
    ):
        revoke_dataset_key_grants(installation=replaced_installation)
    action = "tablet.adopted"
    request.completed_at = now
    request.completion_replay_valid_until = now + COMPLETION_REPLAY_DURATION
    request.save(update_fields=("completed_at", "completion_replay_valid_until"))
    invitation.used_at = now
    invitation.save(update_fields=("used_at",))
    # Adoption establishes a durable installation. It does not silently
    # commission an INACTIVE physical asset; activation remains an explicit
    # administrator lifecycle action. Re-provisioning an already ACTIVE asset
    # therefore preserves its state without changing assignment semantics.
    record_event(
        action=action,
        actor_user=invitation.created_by,
        department=tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
        metadata={"hpke_key_fingerprint": installation.hpke_key_fingerprint},
    )
    return installation, credential


@transaction.atomic
def _replay_successful_completion(
    *, request_id: UUID, challenge_response: bytes
) -> tuple[AppInstallation, str]:
    request = AdoptionRequest.objects.select_for_update(of=("self",)).get(pk=request_id)
    now = timezone.now()
    invitation = AdoptionInvitation.objects.select_for_update(of=("self",)).get(
        pk=request.invitation_id
    )
    installation = (
        AppInstallation.objects.select_for_update(of=("self",))
        .select_related("tablet__department")
        .get(installation_uuid=request.installation_uuid)
    )
    if (
        request.completed_at is None
        or request.completion_replay_valid_until is None
        or request.completion_replay_valid_until <= now
        or request.completion_replay_invalidated_at is not None
        or invitation.revoked_at is not None
        or not hmac.compare_digest(bytes(request.expected_hmac_digest), challenge_response)
    ):
        raise TabletError(
            "Completion recovery is not available.", code="adoption_request_completed"
        )
    if installation.status in (AppInstallation.Status.REVOKED, AppInstallation.Status.REPLACED):
        raise TabletError("Completion recovery is not available.", code="installation_inactive")
    credential = generate_credential()
    installation.credential_hash = _secret_digest(credential)
    installation.save(update_fields=("credential_hash",))
    record_event(
        action="tablet.completion_recovered",
        actor_user=invitation.created_by,
        department=installation.tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
        metadata={"mode": "adoption"},
    )
    return installation, credential


def complete_adoption(
    *, request_id: UUID, challenge_response: bytes, confirmed: bool
) -> tuple[AppInstallation, str]:
    if not confirmed:
        raise TabletError("Adoption was not confirmed.", code="invalid_request")
    request = AdoptionRequest.objects.select_related("invitation__tablet__department").get(
        pk=request_id
    )
    invitation = request.invitation
    now = timezone.now()
    if request.completed_at:
        return _replay_successful_completion(
            request_id=request_id, challenge_response=challenge_response
        )
    if request.expires_at <= now:
        raise TabletError("Adoption request is expired.", code="adoption_request_expired")
    if request.failed_attempt_count >= MAX_FAILED_ATTEMPTS:
        raise TabletError(
            "Adoption request has reached the maximum failed attempts.",
            code="adoption_attempt_limit_reached",
        )
    if invitation is None:
        raise TabletError("Adoption request has no invitation.")
    if invitation.used_at:
        raise TabletError("Invitation has already been used.", code="invitation_invalid")
    if invitation.revoked_at:
        raise TabletError("Invitation has been revoked.", code="invitation_invalid")
    proof_valid = hmac.compare_digest(bytes(request.expected_hmac_digest), challenge_response)
    if not proof_valid:
        _record_failed_attempt(request_id=request_id)
        raise TabletError("Adoption proof is invalid.", code="adoption_proof_invalid")
    return _complete_successful_adoption(request_id=request_id)


@transaction.atomic
def check_in(
    *,
    installation: AppInstallation,
    credential: str,
    app_version: str | None = None,
    app_build: int | None = None,
    build_supplied: bool = False,
    minimum_app_version=None,
) -> AppInstallation:
    installation = (
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .get(pk=installation.pk)
    )
    now = timezone.now()
    if not verify_credential(installation=installation, credential=credential):
        raise PermissionDenied("Installation credential is invalid.")
    if installation.status not in (AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE):
        raise TabletError("Installation is not active.", code="installation_inactive")
    if installation.tablet.status == Tablet.Status.INACTIVE:
        # Deactivation does not revoke the durable identity.  It permits a
        # heartbeat/control-plane check-in but never renews operational lease
        # authorization or transitions a stale installation back to ACTIVE.
        _require_inactive_control_tablet(tablet=installation.tablet)
        telemetry_updated = update_app_telemetry(
            installation=installation,
            app_version=app_version,
            app_build=app_build,
            build_supplied=build_supplied,
            now=now,
        )
        installation.last_successful_check_in_at = now
        inactive_fields = ["last_successful_check_in_at"]
        if telemetry_updated:
            inactive_fields.extend(("app_version", "app_build", "app_version_seen_at"))
        installation.save(update_fields=inactive_fields)
        if (
            minimum_app_version is not None
            and parse_app_version(installation.app_version) < minimum_app_version
        ):
            installation.compatibility_blocked = True
        return installation
    if (
        installation.status == AppInstallation.Status.ACTIVE
        and installation.authorization_valid_until <= now
    ):
        _mark_stale(installation, now)
    telemetry_updated = update_app_telemetry(
        installation=installation,
        app_version=app_version,
        app_build=app_build,
        build_supplied=build_supplied,
        now=now,
    )
    if (
        minimum_app_version is not None
        and parse_app_version(installation.app_version) < minimum_app_version
    ):
        compatibility_fields: list[str] = []
        if telemetry_updated:
            compatibility_fields.extend(("app_version", "app_build", "app_version_seen_at"))
        if compatibility_fields:
            installation.save(update_fields=compatibility_fields)
        installation.compatibility_blocked = True
        return installation
    if installation.status == AppInstallation.Status.STALE:
        _recover_stale_installation(installation=installation, now=now)
    else:
        _require_active_operational_tablet(tablet=installation.tablet, now=now)
        installation.last_successful_check_in_at = now
    if _renew_lease_if_due(installation=installation, now=now):
        fields = ["last_successful_check_in_at", "authorization_valid_until"]
    else:
        fields = ["last_successful_check_in_at"]
    if telemetry_updated:
        fields.extend(("app_version", "app_build", "app_version_seen_at"))
    installation.save(update_fields=fields)
    return installation


@transaction.atomic
def refresh_installation_lease(
    *,
    installation: AppInstallation,
    credential: str,
    app_version: str | None = None,
    app_build: int | None = None,
    build_supplied: bool = False,
    minimum_app_version=None,
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
        raise TabletError("Installation is not active.", code="installation_inactive")
    _require_operational_tablet(installation.tablet)
    if not _eligible_for_lease_renewal(installation=installation, now=now):
        raise TabletError(
            "Only an active, authorized installation can be renewed.", code="installation_inactive"
        )
    old_expiry = installation.authorization_valid_until
    target = lease_target(department=installation.tablet.department, now=now)
    installation.authorization_valid_until = max(old_expiry, target)
    installation.last_successful_check_in_at = now
    telemetry_updated = update_app_telemetry(
        installation=installation,
        app_version=app_version,
        app_build=app_build,
        build_supplied=build_supplied,
        now=now,
    )
    if (
        minimum_app_version is not None
        and parse_app_version(installation.app_version) < minimum_app_version
    ):
        fields: list[str] = []
        if telemetry_updated:
            fields.extend(("app_version", "app_build", "app_version_seen_at"))
        if fields:
            installation.save(update_fields=fields)
        installation.compatibility_blocked = True
        return installation
    fields = ["authorization_valid_until", "last_successful_check_in_at"]
    if telemetry_updated:
        fields.extend(("app_version", "app_build", "app_version_seen_at"))
    installation.save(update_fields=fields)
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
    # Installation health only. The physical Tablet asset state is unchanged:
    # a Tablet can remain ACTIVE while its current installation is STALE.
    installation.status = AppInstallation.Status.STALE
    installation.stale_at = now
    installation.save(update_fields=("status", "stale_at"))
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


def _require_asset_transition(tablet: Tablet, new_status: str) -> None:
    if new_status not in ASSET_TRANSITIONS.get(tablet.status, ()):
        raise TabletError("This lifecycle transition is not available for the tablet.")


def _revoke_tablet_installations(*, tablet: Tablet, reason: str, now) -> None:
    """Revoke current installations and their data access for a withdrawn tablet.

    Revocation cuts operational authorization and dataset-key grants while the
    ``purge_provisioned_data`` directive remains reachable through the existing
    narrow post-revocation ``/api/v1/tablet/status`` path.
    """
    AppInstallation.objects.filter(
        tablet=tablet, status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
    ).update(status=AppInstallation.Status.REVOKED, revoked_at=now, revocation_reason=reason[:512])
    AdoptionRequest.objects.filter(
        invitation__tablet=tablet,
        completed_at__isnull=False,
        completion_replay_valid_until__gt=now,
        completion_replay_invalidated_at__isnull=True,
    ).update(completion_replay_invalidated_at=now)
    from apps.publications.manifests import revoke_dataset_key_grants

    for installation in AppInstallation.objects.filter(
        tablet=tablet, status=AppInstallation.Status.REVOKED
    ):
        revoke_dataset_key_grants(installation=installation)
    AdoptionInvitation.objects.filter(
        tablet=tablet, used_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now)


@transaction.atomic
def activate_tablet(*, actor, tablet: Tablet) -> Tablet:
    """INACTIVE → ACTIVE: commission a known asset back into operational service."""
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_asset_transition(tablet, Tablet.Status.ACTIVE)
    now = timezone.now()
    if tablet.department.status != tablet.department.Status.ACTIVE or not tablet.active:
        raise TabletError("Tablet department must be active before activation.")
    installation = (
        AppInstallation.objects.select_for_update()
        .filter(
            tablet=tablet,
            status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE),
        )
        .first()
    )
    if installation is None:
        raise TabletError("Tablet requires a current installation before it can be activated.")
    if not _has_current_operational_assignment(tablet=tablet, now=now):
        raise TabletError("Tablet requires a current active vehicle assignment before activation.")
    tablet.status = Tablet.Status.ACTIVE
    tablet.active = True
    tablet.save(update_fields=("status", "active"))
    record_event(
        action="tablet.activated",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


@transaction.atomic
def deactivate_tablet(*, actor, tablet: Tablet, reason: str = "") -> Tablet:
    """ACTIVE → INACTIVE: withdraw operational access without revoking identity."""
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_asset_transition(tablet, Tablet.Status.INACTIVE)
    from apps.publications.manifests import revoke_dataset_key_grants

    for installation in AppInstallation.objects.filter(
        tablet=tablet, status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
    ):
        revoke_dataset_key_grants(installation=installation)
        # A formerly operational signed manifest must not be reused after the
        # asset is recommissioned: its grants were intentionally invalidated.
        from apps.publications.models import SignedManifest

        SignedManifest.objects.filter(app_installation=installation).update(
            status=SignedManifest.Status.OBSOLETE,
            completed_at=timezone.now(),
            error_message="Tablet asset was deactivated.",
        )
    tablet.status = Tablet.Status.INACTIVE
    tablet.active = True
    tablet.save(update_fields=("status", "active"))
    record_event(
        action="tablet.deactivated",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


@transaction.atomic
def mark_tablet_lost(*, actor, tablet: Tablet, reason: str = "") -> Tablet:
    """ACTIVE/INACTIVE → LOST: hardware unaccounted for; revoke installation access."""
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_asset_transition(tablet, Tablet.Status.LOST)
    now = timezone.now()
    _revoke_tablet_installations(tablet=tablet, reason=reason, now=now)
    tablet.status = Tablet.Status.LOST
    tablet.active = False
    tablet.save(update_fields=("status", "active"))
    record_event(
        action="tablet.lost",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


@transaction.atomic
def recover_tablet(*, actor, tablet: Tablet) -> Tablet:
    """LOST → INACTIVE: a found tablet returns to stock/inspection, never directly ACTIVE."""
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_asset_transition(tablet, Tablet.Status.INACTIVE)
    tablet.status = Tablet.Status.INACTIVE
    tablet.active = True
    tablet.save(update_fields=("status", "active"))
    record_event(
        action="tablet.recovered",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


@transaction.atomic
def retire_tablet(*, actor, tablet: Tablet, reason: str = "") -> Tablet:
    """ACTIVE/INACTIVE → RETIRED: permanent withdrawal; revoke installation access."""
    tablet = Tablet.objects.select_for_update().select_related("department").get(pk=tablet.pk)
    require_department_admin(actor, tablet.department)
    _require_asset_transition(tablet, Tablet.Status.RETIRED)
    now = timezone.now()
    _revoke_tablet_installations(tablet=tablet, reason=reason, now=now)
    tablet.status = Tablet.Status.RETIRED
    tablet.active = False
    tablet.save(update_fields=("status", "active"))
    record_event(
        action="tablet.retired",
        actor_user=actor,
        department=tablet.department,
        target_type="tablet",
        target_uuid=tablet.id,
    )
    return tablet


@transaction.atomic
def revoke_installation(
    *, actor, installation: AppInstallation, reason: str = ""
) -> AppInstallation:
    """Revoke one current installation's authorization without changing the Tablet asset."""
    installation = (
        AppInstallation.objects.select_for_update()
        .select_related("tablet__department")
        .get(pk=installation.pk)
    )
    require_department_admin(actor, installation.tablet.department)
    if installation.status not in (AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE):
        raise TabletError("Only a current installation can be revoked.")
    now = timezone.now()
    installation.status = AppInstallation.Status.REVOKED
    installation.revoked_at = now
    installation.revocation_reason = reason[:512]
    installation.save(update_fields=("status", "revoked_at", "revocation_reason"))
    from apps.publications.manifests import revoke_dataset_key_grants

    revoke_dataset_key_grants(installation=installation)
    record_event(
        action="tablet.installation_revoked",
        actor_user=actor,
        department=installation.tablet.department,
        target_type="app_installation",
        target_uuid=installation.id,
    )
    return installation
