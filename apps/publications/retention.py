"""Bounded, lifecycle-safe publication payload retention.

Publication rows are immutable historical identities.  This module only removes
operational payloads after serializing with the existing scope -> job ->
publication lifecycle lock order.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TypedDict

from django.conf import settings
from django.db import transaction
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from apps.audit.services import record_event
from apps.publications.document_artifacts import release_terminal_document_artifact_references
from apps.publications.manifests import revoke_publication_dataset_key_grants
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import _schedule_artifact_removal


class RetentionResult(TypedDict):
    considered: int
    obsoleted: int
    snapshots_purged: int
    skipped: int


def _retained_predecessor_ids() -> Iterable[object]:
    """Return the two newest usable rollback predecessors in every scope.

    The database calculates the per-scope rank, so candidate discovery does
    not materialize publication history in Python.
    """
    retained = settings.PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS
    return (
        DatasetPublication.objects.filter(
            status=DatasetPublication.Status.SUPERSEDED,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
            artifact_ready=True,
        )
        .annotate(
            predecessor_rank=Window(
                expression=RowNumber(),
                partition_by=(F("scope_state_id"),),
                order_by=F("version_number").desc(),
            )
        )
        .filter(predecessor_rank__lte=retained)
        .values("pk")
    )


def _candidate_ids(*, limit: int, now: datetime) -> list[object]:
    """Fetch at most ``limit`` lightweight, potentially retainable rows."""
    protected_ids = _retained_predecessor_ids()
    superseded_ids = list(
        DatasetPublication.objects.filter(status=DatasetPublication.Status.SUPERSEDED)
        .exclude(pk__in=protected_ids)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    remaining = limit - len(superseded_ids)
    if remaining <= 0:
        return superseded_ids

    cutoff = now - timedelta(days=settings.PUBLICATION_TERMINAL_SNAPSHOT_RETENTION_DAYS)
    terminal_ids = list(
        DatasetPublication.objects.filter(
            status__in=(DatasetPublication.Status.FAILED, DatasetPublication.Status.CANCELLED),
            source_snapshot__isnull=False,
            # This is deliberately an over-inclusive indexed discovery bound.
            # The authoritative job-completed timestamp is rechecked under the
            # scope lock below.
            created_at__lte=cutoff,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:remaining]
    )
    return [*superseded_ids, *terminal_ids]


def _terminal_timestamp(*, publication: DatasetPublication, job: PublicationJob | None) -> datetime:
    """Use the job terminal time when lifecycle history has one.

    Pre-Phase-4 attempt rows can lack a job.  Their immutable creation time is
    the only authoritative historical timestamp and is a conservative fallback.
    """
    return (
        job.completed_at
        if job is not None and job.completed_at is not None
        else publication.created_at
    )


def _is_retained_predecessor(*, scope: DatasetScopeState, publication: DatasetPublication) -> bool:
    retained = list(
        DatasetPublication.objects.filter(
            scope_state=scope,
            status=DatasetPublication.Status.SUPERSEDED,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
            artifact_ready=True,
        )
        .order_by("-version_number")
        .values_list("pk", flat=True)[: settings.PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS]
    )
    return publication.pk in retained


def _live_job(*, scope: DatasetScopeState) -> PublicationJob | None:
    return (
        PublicationJob.objects.select_for_update()
        .filter(
            scope_state=scope,
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        )
        .order_by("created_at")
        .first()
    )


@transaction.atomic
def _process_candidate(*, publication_id: object, now: datetime, dry_run: bool) -> str:
    """Recheck and mutate one candidate under the established lock order."""
    preview = DatasetPublication.objects.filter(pk=publication_id).values("scope_state_id").first()
    if preview is None:
        return "skipped"
    scope = (
        DatasetScopeState.objects.select_for_update().filter(pk=preview["scope_state_id"]).first()
    )
    if scope is None:
        return "skipped"

    # Scope -> job -> publication.  Skip a scope with an active lifecycle
    # operation; the next bounded maintenance pass will reconsider it.
    if _live_job(scope=scope) is not None:
        return "skipped"
    related_job = (
        PublicationJob.objects.select_for_update()
        .filter(scope_state=scope, build_publication_id=publication_id)
        .order_by("-created_at")
        .first()
    )
    publication = (
        DatasetPublication.objects.select_for_update()
        .filter(pk=publication_id, scope_state=scope)
        .first()
    )
    if publication is None:
        return "skipped"

    if publication.status == DatasetPublication.Status.SUPERSEDED:
        if scope.current_published_publication_id == publication.id:
            return "skipped"
        if _is_retained_predecessor(scope=scope, publication=publication):
            return "skipped"
        if dry_run:
            return "obsoleted"
        artifact_path = publication.artifact_path
        revoke_publication_dataset_key_grants(publication=publication)
        publication.status = DatasetPublication.Status.OBSOLETE
        publication.source_snapshot = None
        publication.save(update_fields=("status", "source_snapshot"))
        release_terminal_document_artifact_references(publication=publication)
        if scope.latest_built_publication_id == publication.id:
            scope.latest_built_publication = scope.current_published_publication
            scope.save(update_fields=("latest_built_publication", "updated_at"))
        _schedule_artifact_removal(artifact_path)
        record_event(
            action="publication.retention_obsoleted",
            department=publication.department,
            station=publication.station,
            target_type="dataset_publication",
            target_uuid=publication.id,
            metadata={
                "dataset_type_code": publication.dataset_type_code,
                "version_number": publication.version_number,
            },
        )
        return "obsoleted"

    if publication.status not in (
        DatasetPublication.Status.FAILED,
        DatasetPublication.Status.CANCELLED,
    ):
        return "skipped"
    if publication.source_snapshot is None:
        return "skipped"
    cutoff = now - timedelta(days=settings.PUBLICATION_TERMINAL_SNAPSHOT_RETENTION_DAYS)
    if _terminal_timestamp(publication=publication, job=related_job) > cutoff:
        return "skipped"
    if dry_run:
        return "snapshot_purged"
    publication.source_snapshot = None
    publication.save(update_fields=("source_snapshot",))
    release_terminal_document_artifact_references(publication=publication)
    record_event(
        action="publication.retention_snapshot_purged",
        department=publication.department,
        station=publication.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={
            "dataset_type_code": publication.dataset_type_code,
            "version_number": publication.version_number,
        },
    )
    return "snapshot_purged"


def run_publication_retention(
    *, batch_size: int | None = None, now: datetime | None = None, dry_run: bool = False
) -> RetentionResult:
    """Apply one bounded, idempotent retention pass.

    Candidate discovery is deliberately outside the per-row transactions.  Each
    candidate is re-evaluated under the scope lock, making races with rollback,
    activation, build completion and cancellation safe without a department-wide
    lock or transaction.
    """
    limit = batch_size or settings.PUBLICATION_RETENTION_BATCH_SIZE
    if limit < 1:
        raise ValueError("Publication retention batch size must be positive.")
    now = now or timezone.now()
    result: RetentionResult = {
        "considered": 0,
        "obsoleted": 0,
        "snapshots_purged": 0,
        "skipped": 0,
    }
    for publication_id in _candidate_ids(limit=limit, now=now):
        result["considered"] += 1
        outcome = _process_candidate(publication_id=publication_id, now=now, dry_run=dry_run)
        if outcome == "obsoleted":
            result["obsoleted"] += 1
        elif outcome == "snapshot_purged":
            result["snapshots_purged"] += 1
        else:
            result["skipped"] += 1
    return result
