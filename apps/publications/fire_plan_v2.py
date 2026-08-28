"""Dormant Fire Plan schema-v2 generation construction.

This module deliberately creates only immutable document membership.  It does
not register a live dataset schema, alter the v1 ZIP artifact, or produce a
manifest/key delivery format.
"""

import hashlib
from collections.abc import Sequence

from django.conf import settings
from django.db import transaction

from apps.publications.builders import PublicationBuildError
from apps.publications.document_artifacts import get_or_create_fire_plan_document_artifact
from apps.publications.models import DatasetPublication, PublicationFirePlanArtifactReference
from apps.publications.pdf_bundles import PdfBundleError, read_accepted_pdf
from apps.reference_data.models import FirePlan


def _snapshot_fire_plans(publication: DatasetPublication) -> list[tuple[FirePlan, str]]:
    """Resolve and validate the frozen distributed PDF identities in order."""
    if publication.dataset_type_code != "department_fire_plans" or publication.station_id:
        raise PublicationBuildError(
            "Fire Plan v2 generation requires a department Fire Plan scope."
        )
    snapshot = publication.source_snapshot
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fire_plans"), list):
        raise PublicationBuildError("Frozen Fire Plan source is unavailable.")
    entries = snapshot["fire_plans"]
    identities: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PublicationBuildError("Frozen Fire Plan source is invalid.")
        plan_id, sanitized_pdf_sha256 = entry.get("id"), entry.get("sha256")
        if (
            not isinstance(plan_id, str)
            or not isinstance(sanitized_pdf_sha256, str)
            or len(sanitized_pdf_sha256) != 64
        ):
            raise PublicationBuildError("Frozen Fire Plan source is invalid.")
        identities.append((plan_id, sanitized_pdf_sha256))
    if len({plan_id for plan_id, _ in identities}) != len(identities):
        raise PublicationBuildError("Frozen Fire Plan source contains duplicate plans.")
    plans = {
        str(plan.id): plan
        for plan in FirePlan.objects.filter(
            department_id=publication.department_id,
            id__in=[plan_id for plan_id, _ in identities],
        )
    }
    if len(plans) != len(identities):
        raise PublicationBuildError("Frozen Fire Plan document is no longer available.")
    resolved = [(plans[plan_id], sha256) for plan_id, sha256 in identities]
    if any(plan.sha256 != sha256 for plan, sha256 in resolved):
        raise PublicationBuildError(
            "Accepted Fire Plan document hash does not match frozen metadata."
        )
    return resolved


def build_fire_plan_v2_generation(
    *, publication: DatasetPublication
) -> Sequence[PublicationFirePlanArtifactReference]:
    """Create the complete immutable membership for one frozen v2 candidate.

    The caller retains responsibility for existing publication lifecycle
    decisions.  This function never activates a publication and only accepts
    a BUILDING attempt, so a staged/cancelled/terminal row cannot gain a v2
    generation membership.
    """
    if publication.status != DatasetPublication.Status.BUILDING:
        raise PublicationBuildError("Fire Plan v2 generation requires a building publication.")
    snapshot_plans = _snapshot_fire_plans(publication)
    expected = {plan.id: sha256 for plan, sha256 in snapshot_plans}
    existing = list(
        PublicationFirePlanArtifactReference.objects.filter(publication=publication).select_related(
            "document_artifact"
        )
    )
    if existing:
        if len(existing) != len(expected) or {
            reference.fire_plan_id for reference in existing
        } != set(expected):
            raise PublicationBuildError("Fire Plan v2 generation membership is incomplete.")
        if any(
            reference.document_artifact.sanitized_pdf_sha256 != expected[reference.fire_plan_id]
            for reference in existing
        ):
            raise PublicationBuildError(
                "Fire Plan v2 generation membership does not match frozen content."
            )
        by_plan = {reference.fire_plan_id: reference for reference in existing}
        return tuple(by_plan[plan.id] for plan, _ in snapshot_plans)

    # The outer transaction makes reference membership all-or-nothing. A
    # filesystem-promoted artifact that loses a later database race is safe as
    # an unreachable Stage A artifact; it can never make this generation live.
    with transaction.atomic():
        references = []
        for plan, sanitized_pdf_sha256 in snapshot_plans:
            try:
                sanitized_pdf = read_accepted_pdf(
                    document_key=plan.document_key,
                    accepted_root=settings.REFERENCE_DATA_ACCEPTED_ROOT,
                )
            except PdfBundleError as error:
                raise PublicationBuildError(
                    "Accepted Fire Plan document is unavailable."
                ) from error
            if hashlib.sha256(sanitized_pdf).hexdigest() != sanitized_pdf_sha256:
                raise PublicationBuildError(
                    "Accepted Fire Plan document hash does not match frozen metadata."
                )
            artifact, _ = get_or_create_fire_plan_document_artifact(
                fire_plan=plan, sanitized_pdf=sanitized_pdf
            )
            references.append(
                PublicationFirePlanArtifactReference.objects.create(
                    publication=publication,
                    fire_plan=plan,
                    document_artifact=artifact,
                )
            )
    return tuple(references)
