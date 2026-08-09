"""Publication-worker-only CEK grant and manifest signing operations."""

import base64

from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.publications.artifacts import ArtifactError, _credential
from apps.publications.hpke import HPKE_CIPHERSUITE, HPKEContext, hpke_seal, parse_p256_public_key
from apps.publications.manifests import (
    ManifestError,
    authorized_publications,
    canonical_manifest_payload,
    manifest_state_hash,
    request_dataset_key_grant,
)
from apps.publications.models import DatasetKeyGrant, SignedManifest
from apps.publications.registry import get_dataset_definition


class KeyGrantError(ValueError):
    pass


def sign_manifest_payload(*, payload: dict[str, object]) -> bytes:
    """Sign a canonical payload in the worker context only."""
    signing_key = _credential(settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH, "signing")
    if len(signing_key) != 32:
        raise KeyGrantError("Publication Ed25519 private key must be exactly 32 bytes.")
    return Ed25519PrivateKey.from_private_bytes(signing_key).sign(
        canonical_manifest_payload(payload)
    )


@transaction.atomic
def claim_next_dataset_key_grant() -> DatasetKeyGrant | None:
    grant = (
        DatasetKeyGrant.objects.select_for_update(skip_locked=True)
        .filter(status=DatasetKeyGrant.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if grant is None:
        return None
    grant.status = DatasetKeyGrant.Status.RUNNING
    grant.error_message = ""
    grant.save(update_fields=("status", "error_message"))
    return grant


def process_next_dataset_key_grant() -> DatasetKeyGrant | None:
    grant = claim_next_dataset_key_grant()
    if grant is None:
        return None
    return build_claimed_dataset_key_grant(grant_id=grant.id)


@transaction.atomic
def build_claimed_dataset_key_grant(*, grant_id) -> DatasetKeyGrant:
    grant = (
        DatasetKeyGrant.objects.select_for_update()
        .select_related(
            "publication__department", "publication__station", "app_installation__tablet"
        )
        .get(pk=grant_id)
    )
    if grant.status != DatasetKeyGrant.Status.RUNNING:
        return grant
    now = timezone.now()
    try:
        installation, _, publications = authorized_publications(
            installation=grant.app_installation, now=now
        )
        publication = grant.publication
        if publication not in publications:
            raise ManifestError("Installation is not authorized for this publication.")
        kek = _credential(settings.PUBLICATION_KEK_CREDENTIAL_PATH, "KEK")
        if len(kek) != 32:
            raise KeyGrantError("Publication KEK must be exactly 32 bytes.")
        cek = keywrap.aes_key_unwrap(kek, bytes(publication.artifact_wrapped_cek))
        context = HPKEContext(
            publication_id=publication.id,
            installation_id=installation.id,
            tablet_id=installation.tablet_id,
            department_id=publication.department_id,
            station_id=publication.station_id,
            dataset_type_code=publication.dataset_type_code,
            version_number=publication.version_number,
            schema_version=publication.schema_version,
            ciphertext_sha256=publication.artifact_sha256,
        )
        encapsulated_key, wrapped_content_key = hpke_seal(
            plaintext=cek,
            recipient_public_key=parse_p256_public_key(bytes(installation.hpke_public_key)),
            context=context,
        )
    except ManifestError as error:
        grant.status = DatasetKeyGrant.Status.REVOKED
        grant.completed_at = now
        grant.revoked_at = now
        grant.error_message = str(error)[:512]
        grant.save(update_fields=("status", "completed_at", "revoked_at", "error_message"))
        return grant
    except (ArtifactError, KeyGrantError, ValueError) as error:
        grant.status = DatasetKeyGrant.Status.FAILED
        grant.completed_at = now
        grant.error_message = str(error)[:512]
        grant.save(update_fields=("status", "completed_at", "error_message"))
        return grant
    grant.status = DatasetKeyGrant.Status.READY
    grant.hpke_ciphersuite = HPKE_CIPHERSUITE
    grant.hpke_encapsulated_key = encapsulated_key
    grant.hpke_wrapped_content_key = wrapped_content_key
    grant.completed_at = now
    grant.save(
        update_fields=(
            "status",
            "hpke_ciphersuite",
            "hpke_encapsulated_key",
            "hpke_wrapped_content_key",
            "completed_at",
        )
    )
    return grant


@transaction.atomic
def claim_next_signed_manifest() -> SignedManifest | None:
    manifest = (
        SignedManifest.objects.select_for_update(skip_locked=True)
        .filter(status=SignedManifest.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if manifest is None:
        return None
    manifest.status = SignedManifest.Status.RUNNING
    manifest.error_message = ""
    manifest.save(update_fields=("status", "error_message"))
    return manifest


def _manifest_payload(*, installation, vehicle, publications, grants, generation, now):
    datasets = []
    for publication, grant in zip(publications, grants, strict=True):
        definition = get_dataset_definition(publication.dataset_type_code)
        datasets.append(
            {
                "publication_id": str(publication.id),
                "type": publication.dataset_type_code,
                "scope": definition.scope,
                "version": publication.version_number,
                "schema_version": publication.schema_version,
                "required": definition.required,
                "minimum_app_version": definition.minimum_app_version,
                "artifact_format": definition.artifact_format,
                "encrypted_size": publication.artifact_size,
                "ciphertext_sha256": publication.artifact_sha256,
                "content_encryption_algorithm": publication.artifact_encryption_algorithm,
                "download_url": f"/api/v1/tablet/datasets/{publication.id}/download",
                "key_grant": {
                    "scheme": "HPKE",
                    "ciphersuite": grant.hpke_ciphersuite,
                    "encapsulated_key": base64.b64encode(bytes(grant.hpke_encapsulated_key)).decode(
                        "ascii"
                    ),
                    "wrapped_content_key": base64.b64encode(
                        bytes(grant.hpke_wrapped_content_key)
                    ).decode("ascii"),
                },
            }
        )
    return {
        "manifest_generation": generation,
        "generated_at": now.isoformat(),
        "authorization_valid_until": installation.authorization_valid_until.isoformat(),
        "configuration": {
            "installation_id": str(installation.id),
            "tablet_id": str(installation.tablet_id),
            "department_id": str(installation.tablet.department_id),
            "station_id": str(vehicle.station_id),
            "vehicle_id": str(vehicle.id),
        },
        "datasets": datasets,
    }


def process_next_signed_manifest() -> SignedManifest | None:
    manifest = claim_next_signed_manifest()
    if manifest is None:
        return None
    return build_claimed_signed_manifest(manifest_id=manifest.id)


@transaction.atomic
def build_claimed_signed_manifest(*, manifest_id) -> SignedManifest:
    manifest = (
        SignedManifest.objects.select_for_update()
        .select_related("app_installation")
        .get(pk=manifest_id)
    )
    if manifest.status != SignedManifest.Status.RUNNING:
        return manifest
    now = timezone.now()
    try:
        installation, vehicle, publications = authorized_publications(
            installation=manifest.app_installation, now=now
        )
        grants = [
            request_dataset_key_grant(publication=publication, installation=installation)
            for publication in publications
        ]
        state_hash = manifest_state_hash(
            installation=installation,
            vehicle=vehicle,
            publications=publications,
            generation=manifest.generation,
        )
        if state_hash != manifest.state_hash:
            manifest.status = SignedManifest.Status.OBSOLETE
            manifest.completed_at = now
            manifest.error_message = "Manifest authorization or publication state changed."
            manifest.save(update_fields=("status", "completed_at", "error_message"))
            return manifest
        if any(grant.status != DatasetKeyGrant.Status.READY for grant in grants):
            manifest.status = SignedManifest.Status.PENDING
            manifest.save(update_fields=("status",))
            return manifest
        payload = _manifest_payload(
            installation=installation,
            vehicle=vehicle,
            publications=publications,
            grants=grants,
            generation=manifest.generation,
            now=now,
        )
        signature = sign_manifest_payload(payload=payload)
    except ManifestError as error:
        manifest.status = SignedManifest.Status.OBSOLETE
        manifest.completed_at = now
        manifest.error_message = str(error)[:512]
        manifest.save(update_fields=("status", "completed_at", "error_message"))
        return manifest
    except (ArtifactError, KeyGrantError, ValueError) as error:
        manifest.status = SignedManifest.Status.FAILED
        manifest.completed_at = now
        manifest.error_message = str(error)[:512]
        manifest.save(update_fields=("status", "completed_at", "error_message"))
        return manifest
    manifest.status = SignedManifest.Status.READY
    manifest.payload = payload
    manifest.signature = signature
    manifest.signature_algorithm = "Ed25519"
    manifest.signing_key_version = settings.PUBLICATION_SIGNING_KEY_VERSION
    manifest.completed_at = now
    manifest.save(
        update_fields=(
            "status",
            "payload",
            "signature",
            "signature_algorithm",
            "signing_key_version",
            "completed_at",
        )
    )
    return manifest
