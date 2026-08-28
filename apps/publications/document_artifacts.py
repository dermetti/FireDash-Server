"""Worker-side creation and reuse of immutable Fire Plan PDF artifacts."""

import hashlib
import logging

from django.db import IntegrityError, transaction

from apps.publications.artifacts import (
    ArtifactError,
    build_encrypted_document_artifact,
    remove_artifact_path,
)
from apps.publications.models import FirePlanDocumentArtifact

logger = logging.getLogger(__name__)


def get_or_create_fire_plan_document_artifact(*, fire_plan, sanitized_pdf: bytes):
    """Return the one immutable artifact for a canonical plan/content hash.

    The unique database constraint resolves concurrent workers.  A losing
    contender removes only its own identity-addressed promoted ciphertext.
    """
    sanitized_pdf_sha256 = hashlib.sha256(sanitized_pdf).hexdigest()
    existing = FirePlanDocumentArtifact.objects.filter(
        fire_plan=fire_plan, sanitized_pdf_sha256=sanitized_pdf_sha256
    ).first()
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
        winner = FirePlanDocumentArtifact.objects.filter(
            fire_plan=fire_plan, sanitized_pdf_sha256=sanitized_pdf_sha256
        ).first()
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
