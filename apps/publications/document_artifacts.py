"""Worker-side creation and reuse of immutable Fire Plan PDF artifacts."""

import hashlib
import logging

from django.db import IntegrityError, transaction

from apps.publications.artifacts import (
    ArtifactError,
    build_encrypted_document_artifact,
    remove_artifact_path,
)
from apps.publications.models import (
    FirePlanDocumentArtifact,
    FirePlanDocumentArtifactCleanup,
    PublicationFirePlanArtifactReference,
)

logger = logging.getLogger(__name__)


def get_or_create_fire_plan_document_artifact(*, fire_plan, sanitized_pdf: bytes):
    """Return the one immutable artifact for a canonical plan/content hash.

    The unique database constraint resolves concurrent workers.  A losing
    contender removes only its own identity-addressed promoted ciphertext.
    """
    sanitized_pdf_sha256 = hashlib.sha256(sanitized_pdf).hexdigest()
    # The row lock is retained by an enclosing generation transaction.  It
    # serializes reuse with retention deleting the final terminal reference.
    with transaction.atomic():
        existing = (
            FirePlanDocumentArtifact.objects.select_for_update()
            .filter(fire_plan=fire_plan, sanitized_pdf_sha256=sanitized_pdf_sha256)
            .first()
        )
        if existing is not None:
            return existing, False

    candidate = FirePlanDocumentArtifact(fire_plan=fire_plan)
    metadata = build_encrypted_document_artifact(
        artifact_id=candidate.id, fire_plan_id=fire_plan.id, sanitized_pdf=sanitized_pdf
    )
    for field, value in metadata.items():
        setattr(candidate, field, value)
    try:
        candidate.full_clean()
        with transaction.atomic():
            candidate.save(force_insert=True)
    except IntegrityError:
        winner = (
            FirePlanDocumentArtifact.objects.select_for_update()
            .filter(fire_plan=fire_plan, sanitized_pdf_sha256=sanitized_pdf_sha256)
            .first()
        )
        # The uniqueness race is expected.  Never remove the winner's path:
        # this candidate path includes its fresh server-generated UUID.
        try:
            remove_artifact_path(candidate.artifact_path)
        except (ArtifactError, OSError) as error:
            logger.warning("Document artifact cleanup deferred for %s: %s", candidate.id, error)
        if winner is None:
            raise
        return winner, False
    return candidate, True


def _schedule_document_artifact_removal(*, artifact_id, artifact_path: str) -> None:
    """Persist and attempt physical removal only after the DB transition commits."""
    cleanup, _ = FirePlanDocumentArtifactCleanup.objects.get_or_create(
        artifact_id=artifact_id,
        defaults={"artifact_path": artifact_path},
    )

    def remove_after_commit() -> None:
        try:
            remove_artifact_path(cleanup.artifact_path)
        except (ArtifactError, OSError) as error:
            logger.warning("Document artifact cleanup deferred for %s: %s", artifact_id, error)
            return
        FirePlanDocumentArtifactCleanup.objects.filter(pk=cleanup.pk).delete()

    transaction.on_commit(remove_after_commit)


def release_terminal_document_artifact_references(*, publication) -> int:
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
    references = list(
        PublicationFirePlanArtifactReference.objects.select_for_update()
        .filter(publication=publication)
        .values_list("document_artifact_id", flat=True)
    )
    if not references:
        return 0
    artifacts = list(
        FirePlanDocumentArtifact.objects.select_for_update()
        .filter(id__in=references)
        .order_by("id")
    )
    PublicationFirePlanArtifactReference.objects.filter(publication=publication).delete()
    queued = 0
    for artifact in artifacts:
        # This recheck happens in the same transaction while the artifact row
        # is locked.  A concurrent reference creator must serialize on it.
        if PublicationFirePlanArtifactReference.objects.filter(
            document_artifact=artifact
        ).exists():
            continue
        artifact_id, artifact_path = artifact.id, artifact.artifact_path
        artifact.delete()
        _schedule_document_artifact_removal(
            artifact_id=artifact_id, artifact_path=artifact_path
        )
        queued += 1
    return queued


def cleanup_unreferenced_document_artifacts(*, batch_size: int = 500) -> int:
    """Retry durable removals and collect orphaned Stage A artifact records."""
    if batch_size < 1:
        raise ValueError("Document artifact cleanup batch size must be positive.")
    removed = 0
    for cleanup in FirePlanDocumentArtifactCleanup.objects.order_by("created_at")[:batch_size]:
        try:
            remove_artifact_path(cleanup.artifact_path)
        except (ArtifactError, OSError) as error:
            logger.warning(
                "Document artifact cleanup deferred for %s: %s", cleanup.artifact_id, error
            )
            continue
        FirePlanDocumentArtifactCleanup.objects.filter(pk=cleanup.pk).delete()
        removed += 1

    candidate_ids = list(
        FirePlanDocumentArtifact.objects.filter(publication_references__isnull=True)
        .order_by("created_at")
        .values_list("id", flat=True)[:batch_size]
    )
    for artifact_id in candidate_ids:
        with transaction.atomic():
            artifact = (
                FirePlanDocumentArtifact.objects.select_for_update().filter(pk=artifact_id).first()
            )
            if artifact is None or PublicationFirePlanArtifactReference.objects.filter(
                document_artifact=artifact
            ).exists():
                continue
            path = artifact.artifact_path
            artifact.delete()
            _schedule_document_artifact_removal(artifact_id=artifact_id, artifact_path=path)
            removed += 1
    return removed
