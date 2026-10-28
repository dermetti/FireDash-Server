"""Durable cleanup and locked retention of generic document artifacts."""

import logging

from django.db import transaction

from apps.publications.artifacts import (
    ArtifactError,
    remove_artifact_path,
)
from apps.publications.cleanup_results import CleanupResult
from apps.publications.models import (
    DocumentArtifact,
    DocumentArtifactCleanup,
    PublicationDocumentArtifactReference,
)

logger = logging.getLogger(__name__)


def _remove_queued_ciphertext(cleanup):
    from apps.publications.paths import document_artifact_relative_path

    if cleanup.artifact_path != document_artifact_relative_path(artifact_id=cleanup.artifact_id):
        raise ArtifactError("Cleanup path must be a generated document path.")
    if (
        DocumentArtifact.objects.filter(pk=cleanup.artifact_id).exists()
        or DocumentArtifact.objects.filter(artifact_path=cleanup.artifact_path).exists()
    ):
        raise ArtifactError("Cleanup cannot remove a stored document artifact.")
    return remove_artifact_path(cleanup.artifact_path)


def _schedule_document_artifact_removal(*, artifact_id, artifact_path: str, result=None) -> None:
    """Persist and attempt physical removal only after the DB transition commits."""
    cleanup, _ = DocumentArtifactCleanup.objects.get_or_create(
        artifact_id=artifact_id,
        defaults={"artifact_path": artifact_path},
    )

    if result is not None:
        result.cleanup_deferred += 1

    def remove_after_commit() -> None:
        if result is not None:
            result.cleanup_deferred -= 1
        _process_cleanup_record(cleanup.pk, result if result is not None else CleanupResult())

    transaction.on_commit(remove_after_commit)


def release_terminal_document_artifact_references(*, publication, cleanup_result=None) -> int:
    """Release one terminal generation and queue only its now-unreachable PDFs.

    Callers must already hold the established scope/job/publication lifecycle
    locks and must set the publication terminal before calling this helper.
    Artifact rows are additionally locked so a concurrent generation either
    acquires the reusable artifact first or observes its deletion and creates
    a new identity; it can never receive a ciphertext queued for removal.
    """
    terminal_statuses = {
        publication.Status.FAILED,
        publication.Status.CANCELLED,
        publication.Status.REJECTED,
        publication.Status.OBSOLETE,
    }
    if publication.status not in terminal_statuses:
        raise ValueError("Only terminal publications can release document artifacts.")
    if publication.dataset_type_code in {"department_files", "station_files"}:
        from apps.file_library.models import PublicationContent

        PublicationContent.objects.filter(publication=publication).delete()
    ids = list(
        PublicationDocumentArtifactReference.objects.select_for_update()
        .filter(publication=publication)
        .values_list("document_artifact_id", flat=True)
    )
    if not ids:
        return 0
    artifacts = list(DocumentArtifact.objects.select_for_update().filter(id__in=ids).order_by("id"))
    PublicationDocumentArtifactReference.objects.filter(publication=publication).delete()
    queued = 0
    for artifact in artifacts:
        if PublicationDocumentArtifactReference.objects.filter(document_artifact=artifact).exists():
            continue
        artifact_id, artifact_path = artifact.id, artifact.artifact_path
        artifact.delete()
        _schedule_document_artifact_removal(
            artifact_id=artifact_id, artifact_path=artifact_path, result=cleanup_result
        )
        queued += 1
    return queued


def _process_cleanup_record(cleanup_id, result):
    """Serialize callbacks and retry workers on the durable queue row."""
    with transaction.atomic():
        cleanup = (
            DocumentArtifactCleanup.objects.select_for_update(skip_locked=True)
            .filter(pk=cleanup_id)
            .first()
        )
        if cleanup is None:
            if DocumentArtifactCleanup.objects.filter(pk=cleanup_id).exists():
                result.cleanup_deferred += 1
            return
        try:
            outcome = _remove_queued_ciphertext(cleanup)
        except (ArtifactError, OSError) as error:
            result.cleanup_failed += 1
            result.cleanup_deferred += 1
            logger.warning(
                "Document artifact cleanup deferred for %s: %s", cleanup.artifact_id, error
            )
            return
        cleanup.delete()
        result.document_cleanup_records_processed += 1
        result.record_file(outcome)


def cleanup_unreferenced_document_artifacts(*, batch_size: int = 500) -> CleanupResult:
    """Retry durable removals and collect orphaned Stage A artifact records."""
    if batch_size < 1:
        raise ValueError("Document artifact cleanup batch size must be positive.")
    result = CleanupResult()
    cleanup_ids = list(
        DocumentArtifactCleanup.objects.order_by("created_at").values_list("pk", flat=True)[
            :batch_size
        ]
    )
    for cleanup_id in cleanup_ids:
        _process_cleanup_record(cleanup_id, result)

    generic_ids = list(
        DocumentArtifact.objects.filter(publication_references__isnull=True)
        .order_by("created_at")
        .values_list("id", flat=True)[:batch_size]
    )
    for artifact_id in generic_ids:
        with transaction.atomic():
            generic_artifact = (
                DocumentArtifact.objects.select_for_update().filter(pk=artifact_id).first()
            )
            if (
                generic_artifact is None
                or PublicationDocumentArtifactReference.objects.filter(
                    document_artifact=generic_artifact
                ).exists()
            ):
                continue
            path = generic_artifact.artifact_path
            generic_artifact.delete()
            _schedule_document_artifact_removal(
                artifact_id=artifact_id, artifact_path=path, result=result
            )
            result.document_orphan_records_removed += 1
    return result
