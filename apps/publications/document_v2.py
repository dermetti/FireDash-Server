# ruff: noqa: E501
"""Reusable worker-only document-manifest-v2 generation machinery.

Dataset adapters provide frozen entries and canonical PDF lookup.  This module
owns immutable encryption, references, generation-key delivery and manifest
construction for datasets which start directly on schema two.
"""

import base64
import hashlib
import re
import secrets

from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.publications.artifacts import (
    ArtifactError,
    _credential,
    build_encrypted_generic_document_artifact,
)
from apps.publications.builders import PublicationBuildError
from apps.publications.document_authorization import _authorized_generation
from apps.publications.document_dataset_adapters import document_dataset_adapter
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    DocumentGenerationHPKEContext,
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


class DocumentV2Error(ValueError):
    pass


def _kek() -> bytes:
    key = _credential(settings.PUBLICATION_KEK_CREDENTIAL_PATH, "KEK")
    if len(key) != 32:
        raise DocumentV2Error("Publication KEK must be exactly 32 bytes.")
    return key


def generation_hpke_context(*, publication, installation) -> DocumentGenerationHPKEContext:
    return DocumentGenerationHPKEContext(
        publication_id=publication.id,
        installation_id=installation.id,
        tablet_id=installation.tablet_id,
        department_id=publication.department_id,
        station_id=publication.station_id,
        dataset_type_code=publication.dataset_type_code,
        version_number=publication.version_number,
        schema_version=2,
    )


def validate_document_artifact_storage(artifact):
    """Stream immutable ciphertext verification before trusting a reusable row."""
    from apps.publications.paths import document_artifact_relative_path

    expected = document_artifact_relative_path(artifact_id=artifact.id)
    if artifact.artifact_path != expected:
        raise DocumentV2Error("Document ciphertext path is invalid.")
    try:
        root = settings.PUBLICATION_ARTIFACT_ROOT.resolve()
        path = (root / expected).resolve()
        path.relative_to(root)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                if size > artifact.ciphertext_size:
                    raise DocumentV2Error(
                        "Document ciphertext size does not match storage metadata."
                    )
                digest.update(chunk)
    except (OSError, ValueError) as error:
        raise DocumentV2Error("Document ciphertext is unavailable or invalid.") from error
    if size != artifact.ciphertext_size or digest.hexdigest() != artifact.ciphertext_sha256:
        raise DocumentV2Error("Document ciphertext integrity check failed.")


@transaction.atomic
def get_or_create_document_artifact(
    *, dataset_type_code, canonical_document_id, sanitized_pdf, created_artifacts=None
):
    digest = hashlib.sha256(sanitized_pdf).hexdigest()
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
        validate_document_artifact_storage(existing)
        return existing, False
    candidate = DocumentArtifact(
        dataset_type_code=dataset_type_code, canonical_document_id=canonical_document_id
    )
    if created_artifacts is not None:
        from apps.publications.paths import document_artifact_relative_path

        created_artifacts.append(
            (candidate.id, document_artifact_relative_path(artifact_id=candidate.id))
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
        with transaction.atomic():
            candidate.save(force_insert=True)
    except IntegrityError:
        from apps.publications.document_artifacts import _schedule_document_artifact_removal

        _schedule_document_artifact_removal(
            artifact_id=candidate.id, artifact_path=candidate.artifact_path
        )
        winner = DocumentArtifact.objects.select_for_update().get(
            dataset_type_code=dataset_type_code,
            canonical_document_id=canonical_document_id,
            sanitized_pdf_sha256=digest,
        )
        validate_document_artifact_storage(winner)
        return winner, False
    return candidate, True


def build_document_v2_generation(*, publication, created_artifacts=None):
    if publication.schema_version != 2:
        raise PublicationBuildError("Document v2 generation requires schema version two.")
    if publication.status != publication.Status.BUILDING:
        raise PublicationBuildError("Document v2 generation requires a building publication.")
    adapter = document_dataset_adapter(dataset_type_code=publication.dataset_type_code)
    entries = adapter.frozen_documents(publication)
    expected = {entry.canonical_document_id: entry.sanitized_pdf_sha256 for entry in entries}
    existing = list(
        PublicationDocumentArtifactReference.objects.filter(publication=publication).select_related(
            "document_artifact"
        )
    )
    # Read and verify accepted bytes even when references/artifacts already exist.
    pdfs = {}
    for entry in entries:
        try:
            pdf = read_accepted_pdf(
                document_key=entry.accepted_document_key,
                accepted_root=settings.REFERENCE_DATA_ACCEPTED_ROOT,
            )
        except PdfBundleError as error:
            raise PublicationBuildError("Accepted document is unavailable.") from error
        if hashlib.sha256(pdf).hexdigest() != entry.sanitized_pdf_sha256:
            raise PublicationBuildError("Accepted document hash does not match frozen metadata.")
        pdfs[entry.canonical_document_id] = pdf
    if existing:
        if (
            len(existing) != len(expected)
            or {ref.canonical_document_id for ref in existing} != set(expected)
            or any(
                ref.document_artifact.canonical_document_id != ref.canonical_document_id
                or ref.document_artifact.dataset_type_code != publication.dataset_type_code
                or ref.document_artifact.sanitized_pdf_sha256 != expected[ref.canonical_document_id]
                for ref in existing
            )
        ):
            raise PublicationBuildError("Document v2 generation membership is incomplete.")
        for ref in existing:
            validate_document_artifact_storage(ref.document_artifact)
        return tuple(existing)
    with transaction.atomic():
        refs = []
        for entry in entries:
            pdf = pdfs[entry.canonical_document_id]
            artifact, _ = get_or_create_document_artifact(
                dataset_type_code=publication.dataset_type_code,
                canonical_document_id=entry.canonical_document_id,
                sanitized_pdf=pdf,
                created_artifacts=created_artifacts,
            )
            refs.append(
                PublicationDocumentArtifactReference.objects.create(
                    publication=publication,
                    canonical_document_id=entry.canonical_document_id,
                    document_artifact=artifact,
                )
            )
    return tuple(refs)


def ensure_generation_key(*, publication):
    existing = DocumentGenerationKey.objects.filter(publication=publication).first()
    if existing:
        return existing
    if publication.status != publication.Status.BUILDING:
        raise DocumentV2Error("A generation key must be built before publication.")
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
    adapter = document_dataset_adapter(dataset_type_code=publication.dataset_type_code)
    entries = {
        str(entry.canonical_document_id): entry for entry in adapter.frozen_documents(publication)
    }
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
        if (
            artifact.sanitized_pdf_sha256 != entry.sanitized_pdf_sha256
            or artifact.dataset_type_code != publication.dataset_type_code
            or artifact.canonical_document_id != ref.canonical_document_id
        ):
            raise DocumentV2Error("Artifact does not match frozen document content.")
        cek = keywrap.aes_key_unwrap(_kek(), bytes(artifact.wrapped_cek))
        documents.append(
            {
                adapter.manifest_metadata_key: entry.metadata,
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
    if publication.dataset_type_code in {"department_files", "station_files"}:
        payload.update(
            {
                key: publication.source_snapshot[key]
                for key in ("library", "folders", "source_revision")
            }
        )
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
    grant, _ = DocumentGenerationKeyGrant.objects.select_for_update().get_or_create(
        publication=publication, app_installation=installation
    )
    if grant.status in (grant.Status.FAILED, grant.Status.REVOKED):
        # Only fresh persisted authorization can request replacement key material.
        grant.status = grant.Status.PENDING
        grant.hpke_ciphersuite = ""
        grant.hpke_encapsulated_key = None
        grant.hpke_wrapped_generation_key = None
        grant.completed_at = None
        grant.revoked_at = None
        grant.error_message = ""
        grant.save(
            update_fields=(
                "status",
                "hpke_ciphersuite",
                "hpke_encapsulated_key",
                "hpke_wrapped_generation_key",
                "completed_at",
                "revoked_at",
                "error_message",
            )
        )
    return grant


def process_next_generation_key_grant():
    with transaction.atomic():
        grant = (
            DocumentGenerationKeyGrant.objects.select_for_update(skip_locked=True)
            .filter(status__in=("PENDING", "RUNNING"))
            .order_by("created_at")
            .first()
        )
        if not grant:
            return None
        grant.status = grant.Status.RUNNING
        grant.save(update_fields=("status",))
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
            _authorized_generation(publication=grant.publication, installation=installation)
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
        except ManifestError as error:
            grant.status, grant.completed_at, grant.revoked_at, grant.error_message = (
                grant.Status.REVOKED,
                timezone.now(),
                timezone.now(),
                str(error)[:512],
            )
            grant.save(update_fields=("status", "completed_at", "revoked_at", "error_message"))
        except (ArtifactError, DocumentV2Error, KeyGrantError, InvalidUnwrap, ValueError) as error:
            grant.status, grant.completed_at, grant.error_message = (
                grant.Status.FAILED,
                timezone.now(),
                str(error)[:512],
            )
            grant.save(update_fields=("status", "completed_at", "error_message"))
        return grant


def validate_ready_document_generation(publication, *, historical=False):
    """Validate persisted generic membership before model-level publication closeout."""
    adapter = document_dataset_adapter(dataset_type_code=publication.dataset_type_code)
    # Historical delivery is bound to its immutable snapshot and encrypted
    # references, not the mutable accepted PDF source used to build new versions.
    from apps.publications.document_dataset_adapters import DocumentDatasetAdapter

    frozen = (
        adapter.frozen_documents(publication, resolve_accepted_source=False)
        if historical and isinstance(adapter, DocumentDatasetAdapter)
        else adapter.frozen_documents(publication)
    )
    entries = {str(e.canonical_document_id): e for e in frozen}
    # Only the strict read-only batch assessor opts into its fresh full projection.
    # Ordinary activation/build callers continue to query persisted state.
    batched = getattr(publication, "_restore_validation_batch", False)
    cache = publication._state.fields_cache if batched else {}
    manifest = (
        cache["document_generation_manifest"]
        if "document_generation_manifest" in cache
        else DocumentGenerationManifest.objects.filter(publication=publication).first()
    )
    key = (
        cache["document_generation_key"]
        if "document_generation_key" in cache
        else DocumentGenerationKey.objects.filter(publication=publication).first()
    )
    if (
        not manifest
        or not key
        or publication.artifact_path
        or len(bytes(key.wrapped_key)) != 40
        or key.wrapping_algorithm != "AES-KW-RFC3394"
        or not key.kek_version
        or manifest.signature_algorithm != "Ed25519"
        or len(bytes(manifest.signature)) != 64
        or not manifest.signing_key_version
    ):
        raise DocumentV2Error("Publication requires a complete ready document generation.")
    payload = manifest.payload
    if not isinstance(payload, dict):
        raise DocumentV2Error("Document generation manifest is invalid.")
    if (
        any(
            payload.get(k) != v
            for k, v in {
                "format": "document-generation-v2",
                "dataset_type": publication.dataset_type_code,
                "publication_id": str(publication.id),
                "schema_version": 2,
                "version": publication.version_number,
            }.items()
        )
        or type(payload.get("version")) is not int
        or type(payload.get("schema_version")) is not int
        or not isinstance(payload.get("documents"), list)
    ):
        raise DocumentV2Error("Document generation manifest is invalid.")
    refs = list(
        publication.document_artifact_references.all()
        if batched
        and "document_artifact_references" in getattr(publication, "_prefetched_objects_cache", {})
        else PublicationDocumentArtifactReference.objects.filter(
            publication=publication
        ).select_related("document_artifact")
    )
    if (
        len(refs) != len(entries)
        or len(payload["documents"]) != len(entries)
        or not all(isinstance(document, dict) for document in payload["documents"])
    ):
        raise DocumentV2Error("Document v2 generation membership is incomplete.")
    documents_by_artifact_id = {}
    for document in payload["documents"]:
        identity = document.get("artifact_id")
        if not isinstance(identity, str) or identity in documents_by_artifact_id:
            raise DocumentV2Error("Invalid or duplicate document artifact identity.")
        documents_by_artifact_id[identity] = document
    if set(documents_by_artifact_id) != {str(ref.document_artifact_id) for ref in refs}:
        raise DocumentV2Error("Document v2 generation membership is incomplete.")
    for ref in refs:
        artifact = ref.document_artifact
        entry = entries.get(str(ref.canonical_document_id))
        document = documents_by_artifact_id[str(artifact.id)]
        if (
            not entry
            or not artifact.artifact_path
            or artifact.ciphertext_size <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.ciphertext_sha256)
            or len(bytes(artifact.nonce)) != 12
            or len(bytes(artifact.wrapped_cek)) != 40
            or artifact.encryption_algorithm != "AES-256-GCM"
            or artifact.wrapping_algorithm != "AES-KW-RFC3394"
            or not artifact.kek_version
            or len(bytes(artifact.signature)) != 64
            or artifact.signature_algorithm != "Ed25519"
            or not artifact.signing_key_version
            or artifact.dataset_type_code != publication.dataset_type_code
            or artifact.canonical_document_id != ref.canonical_document_id
            or artifact.sanitized_pdf_sha256 != entry.sanitized_pdf_sha256
            or document.get(adapter.manifest_metadata_key) != entry.metadata
            or document.get("ciphertext_sha256") != artifact.ciphertext_sha256
            or type(document.get("ciphertext_size")) is not int
            or document.get("ciphertext_size") != artifact.ciphertext_size
            or document.get("sanitized_pdf_sha256") != artifact.sanitized_pdf_sha256
            or document.get("nonce") != base64.b64encode(bytes(artifact.nonce)).decode("ascii")
            or document.get("download_path")
            != (
                f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact.id}/download"
            )
            or document.get("generation_key_wrapping_algorithm") != "AES-KW-RFC3394"
            or not isinstance(document.get("generation_wrapped_cek"), str)
            or not re.fullmatch(r"[A-Za-z0-9+/]{54}==", document["generation_wrapped_cek"])
            or any(
                document.get(field) != getattr(artifact, field)
                for field in (
                    "encryption_algorithm",
                    "wrapping_algorithm",
                    "kek_version",
                    "signature_algorithm",
                    "signing_key_version",
                )
            )
            or document.get("signature")
            != base64.b64encode(bytes(artifact.signature)).decode("ascii")
        ):
            raise DocumentV2Error("Artifact does not match frozen document content.")
