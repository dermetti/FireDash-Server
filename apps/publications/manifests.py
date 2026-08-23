"""Credential-free manifest selection, grant requests, and canonical serialization."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.publications.feature_services import is_feature_enabled
from apps.publications.models import DatasetKeyGrant, DatasetPublication, SignedManifest
from apps.publications.registry import get_dataset_definition
from apps.publications.signing_keys import (
    SigningKeyConfigurationError,
    active_publication_signing_key,
    publication_signing_public_key_for_version,
)
from apps.tablets.models import AppInstallation


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestRequest:
    payload: dict[str, object] | None
    unavailable: bool
    request_id: UUID | None = None


def canonical_manifest_payload(payload: dict[str, object]) -> bytes:
    """Serialize the unsigned manifest exactly as covered by Ed25519."""
    if "signature" in payload:
        raise ManifestError("Manifest signature is not part of the signed payload.")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def attach_manifest_signature(*, payload: dict[str, object], signature: bytes) -> dict[str, object]:
    signed = dict(payload)
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def publication_signing_public_key() -> bytes:
    """Load the active public half without accessing worker credentials."""
    try:
        public_key = active_publication_signing_key()
    except SigningKeyConfigurationError as error:
        raise ManifestError(str(error)) from error
    return public_key


def publication_signing_public_key_for_requested_version(version: str) -> bytes:
    """Load one exact public ring entry for the authenticated tablet API."""
    try:
        return publication_signing_public_key_for_version(version)
    except SigningKeyConfigurationError as error:
        raise ManifestError(str(error)) from error


def manifest_response_etag(payload: dict[str, object]) -> str:
    """Return the response ETag, which intentionally excludes only generated_at."""
    etag_payload = {key: value for key, value in payload.items() if key != "generated_at"}
    return (
        '"'
        + hashlib.sha256(
            json.dumps(etag_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        + '"'
    )


def manifest_state_hash(
    *,
    installation: AppInstallation,
    vehicle,
    publications: list[DatasetPublication],
    generation: int,
) -> str:
    """Hash the authorization and publication state that coalesces manifest work."""
    state = {
        "generation": generation,
        # Not a wire field: prevent an inactive empty scope from coalescing
        # with an active empty scope for the same installation/configuration.
        "tablet_asset_state": installation.tablet.status,
        "signing_key_version": settings.PUBLICATION_SIGNING_KEY_VERSION,
        "authorization_valid_until": installation.authorization_valid_until.isoformat(),
        "configuration": {
            "installation_id": str(installation.id),
            "tablet_id": str(installation.tablet_id),
            "department_id": str(installation.tablet.department_id),
            "station_id": str(vehicle.station_id) if vehicle is not None else None,
            "vehicle_id": str(vehicle.id) if vehicle is not None else None,
        },
        "datasets": [_publication_manifest_entry(publication) for publication in publications],
    }
    return hashlib.sha256(canonical_manifest_payload(state)).hexdigest()


def _publication_manifest_entry(publication: DatasetPublication) -> dict[str, object]:
    nonce = publication.artifact_nonce
    wrapped_cek = publication.artifact_wrapped_cek
    signature = publication.artifact_signature
    if nonce is None or wrapped_cek is None or signature is None:
        raise ManifestError("Ready publication is missing cryptographic metadata.")
    return {
        "publication_id": str(publication.id),
        "type": publication.dataset_type_code,
        "version": publication.version_number,
        "schema_version": publication.schema_version,
        "artifact_size": publication.artifact_size,
        "ciphertext_sha256": publication.artifact_sha256,
        "content_encryption_algorithm": publication.artifact_encryption_algorithm,
        "content_encryption_nonce": base64.b64encode(nonce).decode("ascii"),
        "content_key_wrapped_for_kek": base64.b64encode(wrapped_cek).decode("ascii"),
        "content_key_wrapping_algorithm": publication.artifact_wrapping_algorithm,
        "content_key_kek_version": publication.artifact_kek_version,
        "artifact_signature": base64.b64encode(signature).decode("ascii"),
        "artifact_signature_algorithm": publication.artifact_signature_algorithm,
        "artifact_signing_key_version": publication.artifact_signing_key_version,
    }


def control_plane_context(*, installation: AppInstallation, now):
    """Resolve authenticated identity/configuration without granting datasets.

    An INACTIVE physical asset remains a known installation so it can obtain the
    signed empty manifest that removes reference-data scope.  Dataset access is
    deliberately handled by the stricter ``_authorization_context`` below.
    """
    installation = AppInstallation.objects.select_related("tablet__department").get(
        pk=installation.pk
    )
    if (
        installation.status != AppInstallation.Status.ACTIVE
        or installation.authorization_valid_until <= now
        or not installation.tablet.active
        or installation.tablet.department.status != installation.tablet.department.Status.ACTIVE
    ):
        raise ManifestError("Installation is not authorized for control-plane synchronization.")
    if installation.tablet.status == installation.tablet.Status.INACTIVE:
        # An inactive asset intentionally has no operational scope.  It can
        # therefore synchronize an empty manifest even after its assignment
        # has been removed; ACTIVE scope is validated below.
        return installation, None
    assignment = (
        TabletVehicleAssignment.objects.select_related("vehicle__station")
        .filter(
            tablet=installation.tablet,
            valid_from__lte=now,
            ended_at__isnull=True,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .filter(vehicle__active=True, vehicle__station__active=True)
        .first()
    )
    if assignment is None or assignment.vehicle.department_id != installation.tablet.department_id:
        raise ManifestError("Installation has no current authorized vehicle assignment.")
    return installation, assignment.vehicle


def _authorization_context(*, installation: AppInstallation, now):
    """Resolve the active operational dataset scope only."""
    installation, vehicle = control_plane_context(installation=installation, now=now)
    if installation.tablet.status != installation.tablet.Status.ACTIVE:
        raise ManifestError("Installation is not authorized for operational datasets.")
    if vehicle is None:
        raise ManifestError("Installation has no current authorized vehicle assignment.")
    return installation, vehicle


def authorized_publications(*, installation: AppInstallation, now=None):
    """Resolve only registered, enabled publications in the derived tablet scope."""
    installation, vehicle = _authorization_context(
        installation=installation, now=now or timezone.now()
    )
    publications = (
        DatasetPublication.objects.filter(
            department_id=installation.tablet.department_id,
            status=DatasetPublication.Status.PUBLISHED,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
        )
        .filter(Q(station__isnull=True) | Q(station=vehicle.station))
        .order_by("dataset_type_code")
    )
    return (
        installation,
        vehicle,
        [
            publication
            for publication in publications
            if is_feature_enabled(
                department=installation.tablet.department,
                feature_code=get_dataset_definition(publication.dataset_type_code).feature_code,
            )
        ],
    )


def manifest_publications(*, installation: AppInstallation, now=None):
    """Resolve the signed-manifest dataset list for active or inactive assets.

    The signed wire manifest includes installation/tablet/assignment IDs and
    authorization expiry, so an empty result cannot be shared across tablets.
    It is nevertheless coalesced by the existing per-installation state hash
    and does not create a DatasetPublication build or DatasetKeyGrant.
    """
    installation, vehicle = control_plane_context(
        installation=installation, now=now or timezone.now()
    )
    if installation.tablet.status == installation.tablet.Status.INACTIVE:
        return installation, vehicle, []
    if installation.tablet.status != installation.tablet.Status.ACTIVE:
        raise ManifestError("Installation is not authorized for a manifest.")
    publications = (
        DatasetPublication.objects.filter(
            department_id=installation.tablet.department_id,
            status=DatasetPublication.Status.PUBLISHED,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
        )
        .filter(Q(station__isnull=True) | Q(station=vehicle.station))
        .order_by("dataset_type_code")
    )
    return (
        installation,
        vehicle,
        [
            publication
            for publication in publications
            if is_feature_enabled(
                department=installation.tablet.department,
                feature_code=get_dataset_definition(publication.dataset_type_code).feature_code,
            )
        ],
    )


@transaction.atomic
def request_dataset_key_grant(
    *,
    publication: DatasetPublication,
    installation: AppInstallation,
    retry_failed: bool = False,
) -> DatasetKeyGrant:
    """Persist an idempotent worker request without handling KEK or CEK material."""
    _, _, publications = authorized_publications(installation=installation)
    if publication.pk not in {candidate.pk for candidate in publications}:
        raise ManifestError("Installation is not authorized for this publication.")
    grant = DatasetKeyGrant.objects.filter(
        publication=publication, app_installation=installation
    ).first()
    if grant is not None:
        if grant.status == DatasetKeyGrant.Status.REVOKED:
            # Deactivation/revocation invalidates the prior HPKE wrapping.
            # A later ACTIVE authorization may safely request a fresh wrapping
            # in the same unique grant row; inactive assets cannot reach this
            # branch because ``authorized_publications`` remains strict.
            grant.status = DatasetKeyGrant.Status.PENDING
            grant.hpke_ciphersuite = ""
            grant.hpke_encapsulated_key = None
            grant.hpke_wrapped_content_key = None
            grant.completed_at = None
            grant.error_message = ""
            grant.revoked_at = None
            grant.save(
                update_fields=(
                    "status",
                    "hpke_ciphersuite",
                    "hpke_encapsulated_key",
                    "hpke_wrapped_content_key",
                    "completed_at",
                    "error_message",
                    "revoked_at",
                )
            )
            return grant
        if retry_failed and grant.status == DatasetKeyGrant.Status.FAILED:
            grant.status = DatasetKeyGrant.Status.PENDING
            grant.completed_at = None
            grant.error_message = ""
            grant.save(update_fields=("status", "completed_at", "error_message"))
        return grant
    try:
        return DatasetKeyGrant.objects.create(
            publication=publication, app_installation=installation
        )
    except IntegrityError:
        return DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)


def revoke_dataset_key_grants(*, installation: AppInstallation) -> int:
    """Make prior HPKE grant material unusable after access is withdrawn."""
    return DatasetKeyGrant.objects.filter(
        app_installation=installation,
        status__in=(
            DatasetKeyGrant.Status.PENDING,
            DatasetKeyGrant.Status.RUNNING,
            DatasetKeyGrant.Status.READY,
            DatasetKeyGrant.Status.FAILED,
        ),
    ).update(status=DatasetKeyGrant.Status.REVOKED, revoked_at=timezone.now())


def request_manifest(
    *, installation: AppInstallation, generation: int = 1, now: datetime | None = None
) -> ManifestRequest:
    """Return only a persisted signed manifest matching the current state.

    This is safe to call in the web process: it only reads publication metadata and
    queues database work. KEK and signing credentials are deliberately not imported.
    """
    now = now or timezone.now()
    with transaction.atomic():
        installation, vehicle, publications = manifest_publications(
            installation=installation, now=now
        )
        for publication in publications:
            request_dataset_key_grant(
                publication=publication, installation=installation, retry_failed=True
            )
        state_hash = manifest_state_hash(
            installation=installation,
            vehicle=vehicle,
            publications=publications,
            generation=generation,
        )
        try:
            manifest, _ = SignedManifest.objects.get_or_create(
                app_installation=installation,
                state_hash=state_hash,
                defaults={"generation": generation},
            )
        except IntegrityError:
            manifest = SignedManifest.objects.get(
                app_installation=installation, state_hash=state_hash
            )
        if manifest.status in (SignedManifest.Status.FAILED, SignedManifest.Status.OBSOLETE):
            # A new tablet manifest request is an explicit, safe retry signal.
            # It can recover a credential outage without a database repair, but
            # the delivery worker never reclaims terminal failures by itself.
            manifest.status = SignedManifest.Status.PENDING
            manifest.completed_at = None
            manifest.error_message = ""
            manifest.save(update_fields=("status", "completed_at", "error_message"))
        if manifest.status == SignedManifest.Status.READY:
            if manifest.signature is None:
                raise ManifestError("Ready manifest has no signature.")
            payload = dict(manifest.payload)
            payload["signature"] = base64.b64encode(manifest.signature).decode("ascii")
            payload["signature_algorithm"] = manifest.signature_algorithm
            payload["signing_key_version"] = manifest.signing_key_version
            return ManifestRequest(payload=payload, unavailable=False, request_id=manifest.id)
        return ManifestRequest(payload=None, unavailable=True, request_id=manifest.id)


def cleanup_signed_manifests(
    *, retention_days: int, batch_size: int = 500, dry_run: bool = False
) -> int:
    """Remove old terminal manifests without touching current active authorization state."""
    if retention_days < 1 or batch_size < 1:
        raise ValueError("Manifest retention and batch size must be positive.")
    cutoff = timezone.now() - timedelta(days=retention_days)
    latest_active_ready = (
        SignedManifest.objects.filter(
            status=SignedManifest.Status.READY,
            app_installation__status=AppInstallation.Status.ACTIVE,
        )
        .order_by("app_installation_id", "-created_at")
        .distinct("app_installation_id")
        .values("id")
    )
    candidates = (
        SignedManifest.objects.filter(
            status__in=(
                SignedManifest.Status.READY,
                SignedManifest.Status.FAILED,
                SignedManifest.Status.OBSOLETE,
            ),
            completed_at__lt=cutoff,
        )
        .exclude(id__in=latest_active_ready)
        .order_by("completed_at")[:batch_size]
    )
    ids = list(candidates.values_list("id", flat=True))
    if dry_run:
        return len(ids)
    with transaction.atomic():
        return SignedManifest.objects.filter(id__in=ids).delete()[0]
