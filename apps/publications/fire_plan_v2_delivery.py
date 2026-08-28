"""Worker-only signing and key delivery for dormant Fire Plan v2 generations."""

import base64
import secrets

from cryptography.hazmat.primitives import keywrap
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.publications.artifacts import ArtifactError, _credential
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    FirePlanGenerationHPKEContext,
    hpke_seal,
    parse_p256_public_key,
)
from apps.publications.manifests import ManifestError
from apps.publications.models import (
    FirePlanGenerationKey,
    FirePlanGenerationKeyGrant,
    FirePlanGenerationManifest,
    PublicationFirePlanArtifactReference,
)
from apps.publications.worker_grants import KeyGrantError, sign_manifest_payload


class FirePlanV2DeliveryError(ValueError):
    pass


def generation_hpke_context(*, publication, installation) -> FirePlanGenerationHPKEContext:
    """Return the complete public binding for one generation-key grant."""
    return FirePlanGenerationHPKEContext(
        publication_id=publication.id,
        installation_id=installation.id,
        tablet_id=installation.tablet_id,
        department_id=publication.department_id,
        station_id=None,
        dataset_type_code=publication.dataset_type_code,
        version_number=publication.version_number,
        schema_version=2,
    )


def _kek() -> bytes:
    key = _credential(settings.PUBLICATION_KEK_CREDENTIAL_PATH, "KEK")
    if len(key) != 32:
        raise FirePlanV2DeliveryError("Publication KEK must be exactly 32 bytes.")
    return key


def ensure_generation_key(*, publication) -> FirePlanGenerationKey:
    """Create or return the random KEK-wrapped key for one generation."""
    existing = FirePlanGenerationKey.objects.filter(publication=publication).first()
    if existing is not None:
        return existing
    key = _kek()
    candidate = FirePlanGenerationKey(
        publication=publication,
        # Keep random generation material out of model construction/logging.
        wrapped_key=keywrap.aes_key_wrap(key, secrets.token_bytes(32)),
    )
    candidate.wrapping_algorithm = "AES-KW-RFC3394"
    candidate.kek_version = settings.PUBLICATION_KEK_VERSION
    candidate.full_clean()
    try:
        with transaction.atomic():
            candidate.save(force_insert=True)
    except IntegrityError:
        return FirePlanGenerationKey.objects.get(publication=publication)
    return candidate


def unwrap_generation_key(*, generation_key: FirePlanGenerationKey) -> bytes:
    key = keywrap.aes_key_unwrap(_kek(), bytes(generation_key.wrapped_key))
    if len(key) != 32:
        raise FirePlanV2DeliveryError("Generation key is invalid.")
    return key


def _frozen_entries(publication) -> dict[str, dict[str, object]]:
    snapshot = publication.source_snapshot
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fire_plans"), list):
        raise FirePlanV2DeliveryError("Frozen Fire Plan source is unavailable.")
    entries = snapshot["fire_plans"]
    if not all(isinstance(entry, dict) and isinstance(entry.get("id"), str) for entry in entries):
        raise FirePlanV2DeliveryError("Frozen Fire Plan source is invalid.")
    result = {str(entry["id"]): entry for entry in entries}
    if len(result) != len(entries):
        raise FirePlanV2DeliveryError("Frozen Fire Plan source contains duplicate plans.")
    return result


def build_fire_plan_v2_manifest(*, publication) -> FirePlanGenerationManifest:
    """Build and sign a deterministic full generation manifest in the worker."""
    entries = _frozen_entries(publication)
    references = list(
        PublicationFirePlanArtifactReference.objects.filter(publication=publication)
        .select_related("document_artifact")
        .order_by("fire_plan_id")
    )
    if len(references) != len(entries) or {str(ref.fire_plan_id) for ref in references} != set(
        entries
    ):
        raise FirePlanV2DeliveryError("Fire Plan v2 generation membership is incomplete.")
    generation_key = ensure_generation_key(publication=publication)
    plaintext_generation_key = unwrap_generation_key(generation_key=generation_key)
    kek = _kek()
    documents = []
    for reference in references:
        artifact = reference.document_artifact
        entry = entries[str(reference.fire_plan_id)]
        if artifact.sanitized_pdf_sha256 != entry.get("sha256"):
            raise FirePlanV2DeliveryError("Artifact does not match frozen Fire Plan content.")
        cek = keywrap.aes_key_unwrap(kek, bytes(artifact.wrapped_cek))
        documents.append(
            {
                "fire_plan": entry,
                "artifact_id": str(artifact.id),
                "sanitized_pdf_sha256": artifact.sanitized_pdf_sha256,
                "ciphertext_sha256": artifact.ciphertext_sha256,
                "ciphertext_size": artifact.ciphertext_size,
                "nonce": base64.b64encode(bytes(artifact.nonce)).decode("ascii"),
                "encryption_algorithm": artifact.encryption_algorithm,
                "wrapping_algorithm": artifact.wrapping_algorithm,
                "kek_version": artifact.kek_version,
                "signature": base64.b64encode(bytes(artifact.signature)).decode("ascii"),
                "signature_algorithm": artifact.signature_algorithm,
                "signing_key_version": artifact.signing_key_version,
                "generation_wrapped_cek": base64.b64encode(
                    keywrap.aes_key_wrap(plaintext_generation_key, cek)
                ).decode("ascii"),
                "generation_key_wrapping_algorithm": "AES-KW-RFC3394",
                "download_path": (
                    f"/api/v1/tablet/fire-plan-generations/{publication.id}/"
                    f"artifacts/{artifact.id}/download"
                ),
            }
        )
    payload = {
        "format": "fire-plan-generation-v2",
        "publication_id": str(publication.id),
        "version": publication.version_number,
        "schema_version": 2,
        "documents": documents,
    }
    signature = sign_manifest_payload(payload=payload)
    manifest, created = FirePlanGenerationManifest.objects.get_or_create(
        publication=publication,
        defaults={
            "payload": payload,
            "signature": signature,
            "signature_algorithm": "Ed25519",
            "signing_key_version": settings.PUBLICATION_SIGNING_KEY_VERSION,
        },
    )
    if not created and (
        manifest.payload != payload
        or bytes(manifest.signature) != signature
        or manifest.signing_key_version != settings.PUBLICATION_SIGNING_KEY_VERSION
    ):
        raise FirePlanV2DeliveryError("Fire Plan v2 generation manifest is immutable.")
    return manifest


def _authorized_generation(*, publication, installation):
    """Reuse existing installation/assignment validation without v1 activation."""
    from apps.publications.manifests import authorized_publications

    active_installation, vehicle, _ = authorized_publications(installation=installation)
    if publication.dataset_type_code != "department_fire_plans" or publication.station_id:
        raise ManifestError("Publication is not an eligible Fire Plan generation.")
    if publication.department_id != active_installation.tablet.department_id or vehicle is None:
        raise ManifestError("Installation is not authorized for this publication.")
    return active_installation


@transaction.atomic
def request_fire_plan_v2_generation_key_grant(*, publication, installation):
    _authorized_generation(publication=publication, installation=installation)
    grant, _ = FirePlanGenerationKeyGrant.objects.get_or_create(
        publication=publication, app_installation=installation
    )
    return grant


@transaction.atomic
def claim_next_fire_plan_v2_generation_key_grant():
    grant = (
        FirePlanGenerationKeyGrant.objects.select_for_update(skip_locked=True)
        .filter(status=FirePlanGenerationKeyGrant.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if grant is not None:
        grant.status = grant.Status.RUNNING
        grant.save(update_fields=("status",))
    return grant


def process_next_fire_plan_v2_generation_key_grant():
    grant = claim_next_fire_plan_v2_generation_key_grant()
    if grant is None:
        return None
    return build_claimed_fire_plan_v2_generation_key_grant(grant_id=grant.id)


@transaction.atomic
def build_claimed_fire_plan_v2_generation_key_grant(*, grant_id):
    grant = (
        FirePlanGenerationKeyGrant.objects.select_for_update()
        .select_related("publication", "app_installation__tablet")
        .get(pk=grant_id)
    )
    if grant.status != grant.Status.RUNNING:
        return grant
    now = timezone.now()
    try:
        installation = _authorized_generation(
            publication=grant.publication, installation=grant.app_installation
        )
        protected_generation_key = ensure_generation_key(publication=grant.publication)
        generation_key = unwrap_generation_key(generation_key=protected_generation_key)
        context = generation_hpke_context(publication=grant.publication, installation=installation)
        enc, wrapped = hpke_seal(
            plaintext=generation_key,
            recipient_public_key=parse_p256_public_key(bytes(installation.hpke_public_key)),
            context=context,
        )
    except ManifestError as error:
        grant.status, grant.revoked_at, grant.completed_at = grant.Status.REVOKED, now, now
        grant.error_message = str(error)[:512]
        grant.save(update_fields=("status", "revoked_at", "completed_at", "error_message"))
        return grant
    except (ArtifactError, FirePlanV2DeliveryError, KeyGrantError, ValueError) as error:
        grant.status, grant.completed_at, grant.error_message = (
            grant.Status.FAILED,
            now,
            str(error)[:512],
        )
        grant.save(update_fields=("status", "completed_at", "error_message"))
        return grant
    grant.status = grant.Status.READY
    grant.hpke_ciphersuite = HPKE_CIPHERSUITE
    grant.hpke_encapsulated_key = enc
    grant.hpke_wrapped_generation_key = wrapped
    grant.completed_at = now
    grant.save(
        update_fields=(
            "status",
            "hpke_ciphersuite",
            "hpke_encapsulated_key",
            "hpke_wrapped_generation_key",
            "completed_at",
        )
    )
    return grant
