"""Shared restore policy. UI checks are batched; activation validates under locks."""

from django.core.exceptions import ObjectDoesNotExist

from apps.publications.builders import PublicationBuildError
from apps.publications.models import DatasetPublication
from apps.publications.registry import get_dataset_definition


def restore_candidates(queryset):
    return queryset.select_related(
        "document_generation_manifest", "document_generation_key"
    ).prefetch_related("document_artifact_references__document_artifact")


def publication_restore_blocked_reason(publication, *, scope, strict=False, historical=True):
    if (
        publication.scope_state_id != scope.id
        or publication.dataset_type_code != scope.dataset_type_code
        or publication.scope_type != scope.scope_type
        or publication.department_id != scope.department_id
        or publication.station_id != scope.station_id
        or (historical and publication.status != DatasetPublication.Status.SUPERSEDED)
    ):
        return "Only a superseded publication in this scope can be restored."
    definition = get_dataset_definition(publication.dataset_type_code)
    document = definition.artifact_format == "document-manifest-v2"
    if publication.schema_version not in definition.supported_schema_versions or (
        document and publication.schema_version != definition.current_schema_version
    ):
        return (
            "Rollback unavailable: legacy document format"
            if document
            else "Unsupported dataset schema"
        )
    if not publication.artifact_ready or publication.artifact_status != "READY":
        return "Publication artifact is not ready."
    if not document:
        return "" if publication.artifact_path else "Publication has no usable artifact."
    from apps.publications.document_v2 import DocumentV2Error, validate_ready_document_generation

    if strict:
        try:
            validate_ready_document_generation(publication, historical=True)
        except (DocumentV2Error, PublicationBuildError) as error:
            return f"Incomplete document generation: {error}"
        return ""
    # Conservative presentation check without reading PDF/ciphertext storage or
    # issuing per-row queries. Services always run the full validator before activation.
    try:
        manifest = publication.document_generation_manifest
        key = publication.document_generation_key
    except ObjectDoesNotExist:
        return "Incomplete document generation"
    payload = manifest.payload
    if (
        publication.artifact_path
        or not isinstance(payload, dict)
        or payload.get("format") != "document-generation-v2"
        or payload.get("dataset_type") != publication.dataset_type_code
        or payload.get("publication_id") != str(publication.id)
        or type(payload.get("version")) is not int
        or payload.get("version") != publication.version_number
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 2
        or not isinstance(payload.get("documents"), list)
        or manifest.signature_algorithm != "Ed25519"
        or len(bytes(manifest.signature)) != 64
        or not manifest.signing_key_version
        or len(bytes(key.wrapped_key)) != 40
        or key.wrapping_algorithm != "AES-KW-RFC3394"
        or not key.kek_version
    ):
        return "Incomplete document generation"
    refs = list(publication.document_artifact_references.all())
    documents = payload["documents"]
    if len(refs) != len(documents) or not all(isinstance(d, dict) for d in documents):
        return "Incomplete document generation"
    artifacts = {str(ref.document_artifact_id) for ref in refs}
    if (
        len(artifacts) != len(documents)
        or {d.get("artifact_id") for d in documents if isinstance(d.get("artifact_id"), str)}
        != artifacts
    ):
        return "Incomplete document generation"
    snapshot = publication.source_snapshot
    if not isinstance(snapshot, dict):
        return "Incomplete document generation"
    metadata_key = {"department_fire_plans": "fire_plan", "department_klgv_plans": "klgv_plan"}.get(
        publication.dataset_type_code, "file"
    )
    entries = snapshot.get(
        {"fire_plan": "fire_plans", "klgv_plan": "klgv_plans", "file": "documents"}[metadata_key]
    )
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries
    ):
        return "Incomplete document generation"
    frozen = {e["id"]: {k: v for k, v in e.items() if k != "content_id"} for e in entries}
    if len(frozen) != len(entries) or len(entries) != len(refs):
        return "Incomplete document generation"
    for ref in refs:
        artifact = ref.document_artifact
        matches = [d for d in documents if d.get("artifact_id") == str(artifact.id)]
        identity = str(ref.canonical_document_id)
        if (
            len(matches) != 1
            or identity not in frozen
            or matches[0].get(metadata_key) != frozen[identity]
            or artifact.canonical_document_id != ref.canonical_document_id
            or artifact.dataset_type_code != publication.dataset_type_code
            or artifact.sanitized_pdf_sha256 != frozen[identity].get("sha256")
            or matches[0].get("ciphertext_sha256") != artifact.ciphertext_sha256
        ):
            return "Incomplete document generation"
    return ""
