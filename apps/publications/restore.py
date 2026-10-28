"""Shared restore policy. UI checks are batched; activation validates under locks."""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Window
from django.db.models.functions import RowNumber

from apps.publications.builders import PublicationBuildError
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    PublicationDocumentArtifactReference,
)
from apps.publications.registry import get_dataset_definition

# Shared narrow projection: no source, summaries, errors or publication ciphertext.
RESTORE_PRESENTATION_FIELDS = (
    "id",
    "scope_state_id",
    "dataset_type_code",
    "scope_type",
    "department_id",
    "station_id",
    "version_number",
    "schema_version",
    "status",
    "artifact_ready",
    "artifact_status",
    "artifact_path",
    "created_at",
    "published_at",
)
DOCUMENT_CANDIDATE_BATCH_SIZE = 8


def restore_candidates(queryset):
    """Load document-only presentation candidates, never an entire mixed history."""
    return (
        queryset.only(
            *RESTORE_PRESENTATION_FIELDS,
            "source_snapshot",
            "document_generation_manifest__payload",
            "document_generation_manifest__signature",
            "document_generation_manifest__signature_algorithm",
            "document_generation_manifest__signing_key_version",
            "document_generation_key__wrapped_key",
            "document_generation_key__wrapping_algorithm",
            "document_generation_key__kek_version",
        )
        .select_related("document_generation_manifest", "document_generation_key")
        .prefetch_related(
            Prefetch(
                "document_artifact_references",
                queryset=PublicationDocumentArtifactReference.objects.select_related(
                    "document_artifact"
                ).only(
                    "id",
                    "publication_id",
                    "canonical_document_id",
                    "document_artifact_id",
                    "document_artifact__id",
                    "document_artifact__canonical_document_id",
                    "document_artifact__dataset_type_code",
                    "document_artifact__sanitized_pdf_sha256",
                    "document_artifact__ciphertext_sha256",
                ),
            )
        )
    )


def scopes_with_eligible_predecessor(scope_ids):
    """Presentation preflight: SQL existence for JSON, bounded batches for documents.

    Stop inspecting a document scope as soon as one valid predecessor is found.
    Missing generations and unsupported schemas are excluded before loading JSON.
    This is deliberately not the locked activation policy used by services.
    """
    scopes = list(
        DatasetScopeState.objects.filter(
            pk__in=scope_ids,
            current_published_publication__isnull=False,
        ).only("id", "dataset_type_code", "scope_type", "department_id", "station_id")
    )
    if not scopes:
        return set()
    definitions = {s.dataset_type_code: get_dataset_definition(s.dataset_type_code) for s in scopes}
    non_document_schemas = Q(pk__in=[])
    document_schemas = Q(pk__in=[])
    for code, definition in definitions.items():
        if definition.artifact_format == "document-manifest-v2":
            if definition.current_schema_version not in definition.supported_schema_versions:
                continue
            document_schemas |= Q(
                dataset_type_code=code, schema_version=definition.current_schema_version
            )
        else:
            non_document_schemas |= Q(
                dataset_type_code=code, schema_version__in=definition.supported_schema_versions
            )
    # Null owners are compared explicitly; SQL NULL = NULL is not true.
    matching_scope = (
        Q(
            dataset_type_code=F("scope_state__dataset_type_code"),
            scope_type=F("scope_state__scope_type"),
        )
        & (
            Q(department_id=F("scope_state__department_id"))
            | Q(department_id__isnull=True, scope_state__department_id__isnull=True)
        )
        & (
            Q(station_id=F("scope_state__station_id"))
            | Q(station_id__isnull=True, scope_state__station_id__isnull=True)
        )
    )
    base = DatasetPublication.objects.filter(
        matching_scope,
        scope_state_id__in=[s.id for s in scopes],
        status="SUPERSEDED",
        artifact_status="READY",
        artifact_ready=True,
        version_number__lt=F("scope_state__current_published_publication__version_number"),
    )
    non_document = base.filter(non_document_schemas, scope_state_id=OuterRef("pk")).exclude(
        artifact_path=""
    )
    eligible = set(
        DatasetScopeState.objects.filter(pk__in=[s.id for s in scopes])
        .filter(Exists(non_document))
        .values_list("pk", flat=True)
    )
    pending = {
        s.id: s
        for s in scopes
        if definitions[s.dataset_type_code].artifact_format == "document-manifest-v2"
    }
    cursors = {}
    while pending:
        bounds = Q(pk__in=[])
        for scope_id in pending:
            bound = Q(scope_state_id=scope_id)
            if scope_id in cursors:
                bound &= Q(version_number__lt=cursors[scope_id])
            bounds |= bound
        batch = list(
            restore_candidates(
                base.filter(
                    document_schemas,
                    bounds,
                    artifact_path="",
                    document_generation_manifest__isnull=False,
                    document_generation_key__isnull=False,
                )
                .annotate(
                    candidate_rank=Window(
                        expression=RowNumber(),
                        partition_by=[F("scope_state_id")],
                        order_by=F("version_number").desc(),
                    )
                )
                .filter(candidate_rank__lte=DOCUMENT_CANDIDATE_BATCH_SIZE)
                .order_by("scope_state_id", "-version_number")
            )
        )
        counts = {}
        for publication in batch:
            scope_id = publication.scope_state_id
            counts[scope_id] = counts.get(scope_id, 0) + 1
            cursors[scope_id] = publication.version_number
            if scope_id not in eligible and not publication_restore_blocked_reason(
                publication, scope=pending[scope_id]
            ):
                eligible.add(scope_id)
        pending = {
            scope_id: scope
            for scope_id, scope in pending.items()
            if scope_id not in eligible and counts.get(scope_id) == DOCUMENT_CANDIDATE_BATCH_SIZE
        }
    return eligible


def publication_restore_metadata_blocked_reason(publication, *, scope, historical=True):
    """Cheap necessary conditions only; document completeness requires the full policy."""
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
    if publication.artifact_path:
        return "Incomplete document generation"
    return ""


def publication_restore_blocked_reason(publication, *, scope, strict=False, historical=True):
    reason = publication_restore_metadata_blocked_reason(
        publication, scope=scope, historical=historical
    )
    if (
        reason
        or get_dataset_definition(publication.dataset_type_code).artifact_format
        != "document-manifest-v2"
    ):
        return reason
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
    documents_by_artifact_id = {}
    for document in documents:
        identity = document.get("artifact_id")
        if not isinstance(identity, str) or identity in documents_by_artifact_id:
            return "Incomplete document generation"
        documents_by_artifact_id[identity] = document
    if set(documents_by_artifact_id) != {str(ref.document_artifact_id) for ref in refs}:
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
        document = documents_by_artifact_id.get(str(artifact.id))
        identity = str(ref.canonical_document_id)
        if (
            document is None
            or identity not in frozen
            or document.get(metadata_key) != frozen[identity]
            or artifact.canonical_document_id != ref.canonical_document_id
            or artifact.dataset_type_code != publication.dataset_type_code
            or artifact.sanitized_pdf_sha256 != frozen[identity].get("sha256")
            or document.get("ciphertext_sha256") != artifact.ciphertext_sha256
        ):
            return "Incomplete document generation"
    return ""


def iter_predecessor_assessments(*, scope):
    """Read-only, strict restore assessments, newest first, with bounded payloads.

    No keys are unwrapped and no storage is read. Lifecycle callers must hold
    the scope lock when using the result to authorize a mutation.
    """
    definition = get_dataset_definition(scope.dataset_type_code)
    document = definition.artifact_format == "document-manifest-v2"
    cursor = None
    while True:
        queryset = DatasetPublication.objects.filter(scope_state=scope, status="SUPERSEDED")
        if cursor is not None:
            queryset = queryset.filter(version_number__lt=cursor)
        batch = list(
            queryset.only(*RESTORE_PRESENTATION_FIELDS).order_by("-version_number")[
                :DOCUMENT_CANDIDATE_BATCH_SIZE
            ]
        )
        if not batch:
            return
        reasons = {p.id: publication_restore_metadata_blocked_reason(p, scope=scope) for p in batch}
        if document:
            from apps.file_library.models import PublicationContent

            ids = [p.id for p in batch if not reasons[p.id]]
            loaded = {
                p.id: p
                for p in DatasetPublication.objects.filter(
                    pk__in=ids,
                    document_generation_manifest__isnull=False,
                    document_generation_key__isnull=False,
                )
                .only(
                    *RESTORE_PRESENTATION_FIELDS,
                    "source_snapshot",
                    "document_generation_manifest",
                    "document_generation_key",
                )
                .select_related("document_generation_manifest", "document_generation_key")
                .prefetch_related(
                    Prefetch(
                        "document_artifact_references",
                        queryset=PublicationDocumentArtifactReference.objects.select_related(
                            "document_artifact"
                        ),
                    ),
                    Prefetch(
                        "publicationcontent_set",
                        queryset=PublicationContent.objects.select_related("content"),
                    ),
                )
            }
            for loaded_publication in loaded.values():
                loaded_publication._restore_validation_batch = True
            for publication in batch:
                if not reasons[publication.id]:
                    reasons[publication.id] = (
                        publication_restore_blocked_reason(
                            loaded[publication.id], scope=scope, strict=True
                        )
                        if publication.id in loaded
                        else "Incomplete document generation"
                    )
        for publication in batch:
            yield publication, reasons[publication.id]
        cursor = batch[-1].version_number
        if len(batch) < DOCUMENT_CANDIDATE_BATCH_SIZE:
            return


def select_restorable_predecessors(*, scope, limit):
    """Return up to limit strictly restorable predecessors without mutation."""
    if limit < 0:
        raise ValueError("Predecessor limit must not be negative.")
    selected = []
    if limit == 0:
        return selected
    for publication, reason in iter_predecessor_assessments(scope=scope):
        if not reason:
            selected.append(publication)
            if len(selected) == limit:
                break
    return selected
