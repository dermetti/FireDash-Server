"""Deterministic publication operational state for the Publications UI.

This is the query/view-model layer that derives one understandable state per
dataset scope so templates never infer business state from several models.
"""

from __future__ import annotations

from typing import Any

from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.publications.builders import source_fingerprint
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition

# Operational state keys.
CURRENT = "CURRENT"
NOT_PUBLISHED = "NOT_PUBLISHED"
UPDATE_QUEUED = "UPDATE_QUEUED"
QUEUED = "QUEUED"
BUILDING = "BUILDING"
READY_TO_PUBLISH = "READY_TO_PUBLISH"
NEEDS_REBUILD = "NEEDS_REBUILD"
FAILED = "FAILED"

STATE_LABELS = {
    CURRENT: "Current",
    NOT_PUBLISHED: "Not published",
    UPDATE_QUEUED: "Update queued",
    QUEUED: "Queued",
    BUILDING: "Building",
    READY_TO_PUBLISH: "Ready to publish",
    NEEDS_REBUILD: "Update required",
    FAILED: "Failed",
}

STATE_BADGE = {
    CURRENT: "success",
    NOT_PUBLISHED: "secondary",
    UPDATE_QUEUED: "info",
    QUEUED: "info",
    BUILDING: "info",
    READY_TO_PUBLISH: "warning",
    NEEDS_REBUILD: "warning",
    FAILED: "danger",
}

CURRENT_UPDATE_LABELS = {
    UPDATE_QUEUED: "Update scheduled",
    QUEUED: "Update scheduled",
    BUILDING: "Building update",
    FAILED: "Update failed",
    NEEDS_REBUILD: "Update required",
}


def compute_scope_state(
    *,
    dirty: bool,
    latest_status: str | None,
    latest_built_status: str | None,
    current_published: bool,
    active_job_status: str | None,
    active_job_not_before,
    now,
    latest_source_fingerprint: str | None = None,
    current_source_fingerprint: str | None = None,
) -> str:
    """Derive the single operational state for one scope from its inputs."""
    if active_job_status == PublicationJob.Status.RUNNING:
        return BUILDING
    if active_job_status == PublicationJob.Status.PENDING:
        if active_job_not_before is not None and active_job_not_before > now:
            return UPDATE_QUEUED
        return QUEUED
    # A publication attempt normally has its corresponding active job. Retain
    # its lifecycle visibility if legacy/recovery work has left that link behind.
    if latest_status == DatasetPublication.Status.BUILDING:
        return BUILDING
    if latest_status == DatasetPublication.Status.STAGED:
        return UPDATE_QUEUED
    if (
        latest_status == DatasetPublication.Status.FAILED
        and dirty
        and (
            # Legacy attempts predate source fingerprints; retain their former
            # failed-state presentation until new attempts establish the value.
            not latest_source_fingerprint or latest_source_fingerprint == current_source_fingerprint
        )
    ):
        return FAILED
    if latest_built_status == DatasetPublication.Status.READY_FOR_REVIEW:
        return READY_TO_PUBLISH
    if dirty:
        return NEEDS_REBUILD
    if current_published:
        return CURRENT
    return NOT_PUBLISHED


def scope_operational_states(
    department, *, now=None, scope_ids: set[object] | None = None
) -> list[dict[str, Any]]:
    """Return one operational-state view model per dataset scope (batched)."""
    now = now or timezone.now()
    latest = DatasetPublication.objects.filter(scope_state=OuterRef("pk")).order_by(
        "-version_number"
    )
    scope_queryset = DatasetScopeState.objects.filter(department=department)
    if scope_ids is not None:
        scope_queryset = scope_queryset.filter(pk__in=scope_ids)
    scopes = (
        scope_queryset.select_related(
            "station", "latest_built_publication", "current_published_publication"
        )
        .annotate(
            latest_status=Subquery(latest.values("status")[:1]),
            latest_publication_id=Subquery(latest.values("id")[:1]),
            latest_publication_version=Subquery(latest.values("version_number")[:1]),
            latest_build_error=Subquery(latest.values("build_error")[:1]),
            latest_source_fingerprint=Subquery(latest.values("source_fingerprint")[:1]),
            latest_built_at=Subquery(latest.values("created_at")[:1]),
        )
        .order_by("dataset_type_code", "station__name")
    )

    active_jobs = {
        job.scope_state_id: job
        for job in PublicationJob.objects.filter(
            department=department,
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        )
        .filter(scope_state__in=scopes)
        .select_related("build_publication")
    }

    rows: list[dict[str, Any]] = []
    for scope in scopes:
        definition = get_dataset_definition(scope.dataset_type_code)
        job = active_jobs.get(scope.id)
        latest_built = scope.latest_built_publication
        current_published = scope.current_published_publication
        current_fingerprint = source_fingerprint(
            definition=definition, department=scope.department, station=scope.station
        )
        if current_published is None:
            dirty = True
        elif current_published.source_fingerprint:
            dirty = current_fingerprint != current_published.source_fingerprint
        else:
            # Historical publications cannot truthfully be reconstructed from
            # their encrypted artifacts. Keep legacy revision behavior only
            # until the next successful publication establishes the fingerprint.
            dirty = scope.source_revision != current_published.source_revision
        state = compute_scope_state(
            dirty=dirty,
            latest_status=scope.latest_status,
            latest_source_fingerprint=scope.latest_source_fingerprint,
            current_source_fingerprint=current_fingerprint,
            latest_built_status=latest_built.status if latest_built else None,
            current_published=current_published is not None,
            active_job_status=job.status if job else None,
            active_job_not_before=job.not_before if job else None,
            now=now,
        )
        rows.append(
            {
                "scope_id": scope.id,
                "dataset_type_code": scope.dataset_type_code,
                "dataset_name": definition.display_name,
                "scope_label": scope.station.name if scope.station else "Department",
                "scope_display_name": (
                    f"{definition.display_name} · {scope.station.short_code}"
                    if scope.station
                    else definition.display_name
                ),
                "state": state,
                "state_label": STATE_LABELS[state],
                "state_badge": STATE_BADGE[state],
                "source_revision": scope.source_revision,
                "source_fingerprint": current_fingerprint,
                "is_dirty": dirty,
                "distributed_version": (
                    current_published.version_number if current_published else None
                ),
                "current_published_at": (
                    current_published.published_at if current_published else None
                ),
                "latest_built_version": latest_built.version_number if latest_built else None,
                "latest_built_status": latest_built.status if latest_built else None,
                "latest_built_publication_id": latest_built.id if latest_built else None,
                "latest_publication_id": scope.latest_publication_id,
                "latest_publication_version": scope.latest_publication_version,
                "current_published_publication_id": (
                    current_published.id if current_published else None
                ),
                "current_update_label": (
                    CURRENT_UPDATE_LABELS.get(state) if current_published else None
                ),
                "current_update_badge": (
                    STATE_BADGE.get(state) if current_published and state != CURRENT else None
                ),
                "build_error": scope.latest_build_error if state == FAILED else None,
                "last_activity": scope.latest_built_at or scope.updated_at,
                "active_job_status": job.status if job else None,
                "active_job_trigger_type": job.trigger_type if job else None,
                "active_job_not_before": job.not_before if job else None,
                "active_job_publication_id": job.build_publication_id if job else None,
                "active_job_publication_version": (
                    job.build_publication.version_number if job and job.build_publication else None
                ),
                "should_poll": (
                    state == BUILDING
                    or (
                        state in (UPDATE_QUEUED, QUEUED)
                        and job is not None
                        # Poll only a staged candidate the worker can claim
                        # now.  This lets automatic pickup become a BUILDING
                        # row without reloading the page, without polling a
                        # delayed data-change job for hours.
                        and (
                            job.trigger_type == PublicationJob.TriggerType.USER_REQUEST
                            or job.not_before is None
                            or job.not_before <= now
                        )
                    )
                ),
            }
        )
    return rows


def operational_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate scope rows into the Status & Context card metrics."""
    summary = {key: 0 for key in STATE_LABELS}
    summary["total"] = len(rows)
    for row in rows:
        summary[row["state"]] += 1
    return summary
