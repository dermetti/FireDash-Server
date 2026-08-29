# ruff: noqa: E501
"""Reusable worker-only document-manifest-v2 generation machinery.

Dataset adapters provide frozen entries and canonical PDF lookup.  This module
owns immutable encryption, references, generation-key delivery and manifest
construction for datasets which start directly on schema two.
"""

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives import keywrap
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.publications.artifacts import (
    ArtifactError,
    _credential,
    build_encrypted_generic_document_artifact,
)
from apps.publications.builders import PublicationBuildError
from apps.publications.fire_plan_v2_delivery import _authorized_generation
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    FirePlanGenerationHPKEContext,
    hpke_seal,
    parse_p256_public_key,
)
from apps.publications.manifests import ManifestError
from apps.publications.models import (
    DocumentArtifact,
    DocumentGenerationKey,
    DocumentGenerationKeyGrant,
    DocumentGenerationManifest,
    PublicationDocumentArtifactReference,
)
from apps.publications.pdf_bundles import PdfBundleError, read_accepted_pdf
from apps.publications.worker_grants import KeyGrantError, sign_manifest_payload
from apps.reference_data.models import KlgvPlan


class DocumentV2Error(ValueError):
    pass


def _kek() -> bytes:
    key = _credential(settings.PUBLICATION_KEK_CREDENTIAL_PATH, "KEK")
    if len(key) != 32:
        raise DocumentV2Error("Publication KEK must be exactly 32 bytes.")
    return key


def generation_hpke_context(*, publication, installation) -> FirePlanGenerationHPKEContext:
    return FirePlanGenerationHPKEContext(
        publication_id=publication.id,
        installation_id=installation.id,
        tablet_id=installation.tablet_id,
        department_id=publication.department_id,
        station_id=publication.station_id,
        dataset_type_code=publication.dataset_type_code,
        version_number=publication.version_number,
        schema_version=2,
    )


def _klgv_entries(publication):
    snapshot = publication.source_snapshot
    if publication.dataset_type_code != "department_klgv_plans" or publication.station_id:
        raise PublicationBuildError("Document v2 adapter is unavailable for this scope.")
    entries = snapshot.get("klgv_plans") if isinstance(snapshot, dict) else None
    if not isinstance(entries, list):
        raise PublicationBuildError("Frozen KLGV source is unavailable.")
    ids = [(entry.get("id"), entry.get("sha256")) for entry in entries if isinstance(entry, dict)]
    if len(ids) != len(entries) or any(
        not isinstance(pk, str) or not isinstance(sha, str) or len(sha) != 64 for pk, sha in ids
    ):
        raise PublicationBuildError("Frozen KLGV source is invalid.")
    if len({pk for pk, _ in ids}) != len(ids):
        raise PublicationBuildError("Frozen KLGV source contains duplicate plans.")
    plans = {
        str(plan.id): plan
        for plan in KlgvPlan.objects.filter(
            department_id=publication.department_id, id__in=[pk for pk, _ in ids]
        )
    }
    if len(plans) != len(ids) or any(plans[pk].sha256 != sha for pk, sha in ids):
        raise PublicationBuildError("Accepted KLGV document hash does not match frozen metadata.")
    by_id = {str(entry["id"]): entry for entry in entries}
    return [(plans[pk], sha, by_id[pk]) for pk, sha in ids]


def get_or_create_document_artifact(*, dataset_type_code, canonical_document_id, sanitized_pdf):
    digest = hashlib.sha256(sanitized_pdf).hexdigest()
    with transaction.atomic():
        existing = (
            DocumentArtifact.objects.select_for_update()
            .filter(
                dataset_type_code=dataset_type_code,
                canonical_document_id=canonical_document_id,
                sanitized_pdf_sha256=digest,
            )
            .first()
        )
        if existing:
            return existing, False
    candidate = DocumentArtifact(
        dataset_type_code=dataset_type_code, canonical_document_id=canonical_document_id
    )
    metadata = build_encrypted_generic_document_artifact(
        artifact_id=candidate.id,
        dataset_type_code=dataset_type_code,
        canonical_document_id=canonical_document_id,
        sanitized_pdf=sanitized_pdf,
    )
    for field, value in metadata.items():
        setattr(candidate, field, value)
    try:
        candidate.save(force_insert=True)
    except IntegrityError:
        from apps.publications.artifacts import remove_artifact_path

        remove_artifact_path(candidate.artifact_path)
        return DocumentArtifact.objects.get(
            dataset_type_code=dataset_type_code,
            canonical_document_id=canonical_document_id,
            sanitized_pdf_sha256=digest,
        ), False
    return candidate, True


def build_document_v2_generation(*, publication):
    if publication.status != publication.Status.BUILDING:
        raise PublicationBuildError("Document v2 generation requires a building publication.")
    entries = _klgv_entries(publication)
    expected = {plan.id: sha for plan, sha, _ in entries}
    existing = list(
        PublicationDocumentArtifactReference.objects.filter(publication=publication).select_related(
            "document_artifact"
        )
    )
    if existing:
        if len(existing) != len(expected) or {ref.canonical_document_id for ref in existing} != set(
            expected
        ):
            raise PublicationBuildError("Document v2 generation membership is incomplete.")
        return tuple(existing)
    with transaction.atomic():
        refs = []
        for plan, sha, _ in entries:
            try:
                pdf = read_accepted_pdf(
                    document_key=plan.path, accepted_root=settings.REFERENCE_DATA_ACCEPTED_ROOT
                )
            except PdfBundleError as error:
                raise PublicationBuildError("Accepted KLGV document is unavailable.") from error
            if hashlib.sha256(pdf).hexdigest() != sha:
                raise PublicationBuildError(
                    "Accepted KLGV document hash does not match frozen metadata."
                )
            artifact, _ = get_or_create_document_artifact(
                dataset_type_code=publication.dataset_type_code,
                canonical_document_id=plan.id,
                sanitized_pdf=pdf,
            )
            refs.append(
                PublicationDocumentArtifactReference.objects.create(
                    publication=publication,
                    canonical_document_id=plan.id,
                    document_artifact=artifact,
                )
            )
    return tuple(refs)


def ensure_generation_key(*, publication):
    existing = DocumentGenerationKey.objects.filter(publication=publication).first()
    if existing:
        return existing
    candidate = DocumentGenerationKey(
        publication=publication,
        wrapped_key=keywrap.aes_key_wrap(_kek(), secrets.token_bytes(32)),
        wrapping_algorithm="AES-KW-RFC3394",
        kek_version=settings.PUBLICATION_KEK_VERSION,
    )
    try:
        candidate.save(force_insert=True)
    except IntegrityError:
        return DocumentGenerationKey.objects.get(publication=publication)
    return candidate


def _unwrap_generation_key(key):
    return keywrap.aes_key_unwrap(_kek(), bytes(key.wrapped_key))


def build_document_v2_manifest(*, publication):
    entries = {str(plan.id): entry for plan, _, entry in _klgv_entries(publication)}
    refs = list(
        PublicationDocumentArtifactReference.objects.filter(publication=publication)
        .select_related("document_artifact")
        .order_by("canonical_document_id")
    )
    if len(refs) != len(entries) or {str(ref.canonical_document_id) for ref in refs} != set(
        entries
    ):
        raise DocumentV2Error("Document v2 generation membership is incomplete.")
    generation_key = _unwrap_generation_key(ensure_generation_key(publication=publication))
    documents = []
    for ref in refs:
        artifact, entry = ref.document_artifact, entries[str(ref.canonical_document_id)]
        if artifact.sanitized_pdf_sha256 != entry.get("sha256"):
            raise DocumentV2Error("Artifact does not match frozen document content.")
        cek = keywrap.aes_key_unwrap(_kek(), bytes(artifact.wrapped_cek))
        documents.append(
            {
                "klgv_plan": entry,
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
                    keywrap.aes_key_wrap(generation_key, cek)
                ).decode("ascii"),
                "generation_key_wrapping_algorithm": "AES-KW-RFC3394",
                "download_path": f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact.id}/download",
            }
        )
    payload = {
        "format": "document-generation-v2",
        "dataset_type": publication.dataset_type_code,
        "publication_id": str(publication.id),
        "version": publication.version_number,
        "schema_version": 2,
        "documents": documents,
    }
    signature = sign_manifest_payload(payload=payload)
    manifest, created = DocumentGenerationManifest.objects.get_or_create(
        publication=publication,
        defaults={
            "payload": payload,
            "signature": signature,
            "signature_algorithm": "Ed25519",
            "signing_key_version": settings.PUBLICATION_SIGNING_KEY_VERSION,
        },
    )
    if not created and (manifest.payload != payload or bytes(manifest.signature) != signature):
        raise DocumentV2Error("Document generation manifest is immutable.")
    return manifest


@transaction.atomic
def request_generation_key_grant(*, publication, installation):
    _authorized_generation(publication=publication, installation=installation)
    grant, _ = DocumentGenerationKeyGrant.objects.get_or_create(
        publication=publication, app_installation=installation
    )
    return grant


def process_next_generation_key_grant():
    with transaction.atomic():
        grant = (
            DocumentGenerationKeyGrant.objects.select_for_update(skip_locked=True)
            .filter(status="PENDING")
            .order_by("created_at")
            .first()
        )
        if not grant:
            return None
        grant.status = grant.Status.RUNNING
        grant.save(update_fields=("status",))
    with transaction.atomic():
        grant = (
            DocumentGenerationKeyGrant.objects.select_for_update()
            .select_related("publication", "app_installation__tablet")
            .get(pk=grant.pk)
        )
        try:
            installation = _authorized_generation(
                publication=grant.publication, installation=grant.app_installation
            )
            enc, wrapped = hpke_seal(
                plaintext=_unwrap_generation_key(
                    ensure_generation_key(publication=grant.publication)
                ),
                recipient_public_key=parse_p256_public_key(bytes(installation.hpke_public_key)),
                context=generation_hpke_context(
                    publication=grant.publication, installation=installation
                ),
            )
            (
                grant.status,
                grant.hpke_ciphersuite,
                grant.hpke_encapsulated_key,
                grant.hpke_wrapped_generation_key,
                grant.completed_at,
            ) = grant.Status.READY, HPKE_CIPHERSUITE, enc, wrapped, timezone.now()
            grant.save(
                update_fields=(
                    "status",
                    "hpke_ciphersuite",
                    "hpke_encapsulated_key",
                    "hpke_wrapped_generation_key",
                    "completed_at",
                )
            )
        except (ArtifactError, DocumentV2Error, KeyGrantError, ManifestError, ValueError) as error:
            grant.status, grant.completed_at, grant.error_message = (
                grant.Status.FAILED,
                timezone.now(),
                str(error)[:512],
            )
            grant.save(update_fields=("status", "completed_at", "error_message"))
        return grant
