"""Credential-free manifest selection, grant requests, and canonical serialization."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.publications.artifacts import ArtifactError, _credential
from apps.publications.feature_services import is_feature_enabled
from apps.publications.models import DatasetKeyGrant, DatasetPublication, SignedManifest
from apps.publications.registry import get_dataset_definition
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
    """Load the separately provisioned public half without accessing worker credentials."""
    try:
        public_key = _credential(
            settings.PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH, "signing public key"
        )
    except ArtifactError as error:
        raise ManifestError(str(error)) from error
    if len(public_key) != 32:
        raise ManifestError("Publication Ed25519 public key must be exactly 32 bytes.")
    return public_key


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
        "signing_key_version": settings.PUBLICATION_SIGNING_KEY_VERSION,
        "authorization_valid_until": installation.authorization_valid_until.isoformat(),
        "configuration": {
            "installation_id": str(installation.id),
            "tablet_id": str(installation.tablet_id),
            "department_id": str(installation.tablet.department_id),
            "station_id": str(vehicle.station_id),
            "vehicle_id": str(vehicle.id),
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


def _authorization_context(*, installation: AppInstallation, now):
    installation = AppInstallation.objects.select_related("tablet__department").get(
        pk=installation.pk
    )
    if (
        installation.status != AppInstallation.Status.ACTIVE
        or installation.authorization_valid_until <= now
        or not installation.tablet.active
        or installation.tablet.department.status != installation.tablet.department.Status.ACTIVE
    ):
        raise ManifestError("Installation is not authorized for a manifest.")
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


@transaction.atomic
def request_dataset_key_grant(
    *, publication: DatasetPublication, installation: AppInstallation
) -> DatasetKeyGrant:
    """Persist an idempotent worker request without handling KEK or CEK material."""
    _, _, publications = authorized_publications(installation=installation)
    if publication.pk not in {candidate.pk for candidate in publications}:
        raise ManifestError("Installation is not authorized for this publication.")
    grant = DatasetKeyGrant.objects.filter(
        publication=publication, app_installation=installation
    ).first()
    if grant is not None:
        return grant
    try:
        return DatasetKeyGrant.objects.create(
            publication=publication, app_installation=installation
        )
    except IntegrityError:
        return DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)


def revoke_dataset_key_grants(*, installation: AppInstallation) -> int:
    """Make grants unusable after an installation is replaced or revoked."""
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
        installation, vehicle, publications = authorized_publications(
            installation=installation, now=now
        )
        for publication in publications:
            request_dataset_key_grant(publication=publication, installation=installation)
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
        if manifest.status == SignedManifest.Status.READY:
            if manifest.signature is None:
                raise ManifestError("Ready manifest has no signature.")
            payload = dict(manifest.payload)
            payload["signature"] = base64.b64encode(manifest.signature).decode("ascii")
            payload["signature_algorithm"] = manifest.signature_algorithm
            payload["signing_key_version"] = manifest.signing_key_version
            return ManifestRequest(payload=payload, unavailable=False, request_id=manifest.id)
        return ManifestRequest(payload=None, unavailable=True, request_id=manifest.id)
