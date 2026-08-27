"""Deterministic publication operational state for the Publications UI.

This is the query/view-model layer that derives one understandable state per
dataset scope so templates never infer business state from several models.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition

STAGED_POLL_GRACE_PERIOD = timedelta(seconds=20)

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


def publication_scope_queryset(department, *, include_station: bool = True):
    """Return the narrow, ordered scope queryset used by the Publications list.

    The list view paginates this queryset before asking for publication state.
    Keeping the base query independent from publication attempts is important:
    a large department must not deserialize every candidate's source snapshot
    merely to render one page of scope rows.
    """
    fields = [
        "id",
        "dataset_type_code",
        "source_revision",
        "current_source_fingerprint",
        "dirty_since",
        "updated_at",
        "station_id",
        "latest_built_publication_id",
        "current_published_publication_id",
    ]
    if include_station:
        fields.extend(("station__id", "station__name", "station__short_code"))
    queryset = DatasetScopeState.objects.filter(department=department).only(*fields)
    if include_station:
        queryset = queryset.select_related("station")
        return queryset.order_by("dataset_type_code", "station__name", "id")
    return queryset.order_by("dataset_type_code", "id")


def _state_publications_and_jobs(scopes: list[DatasetScopeState]):
    """Fetch the state-only publication data for a bounded collection of scopes.

    `DISTINCT ON` is PostgreSQL's efficient one-row-per-scope form.  The
    projection deliberately omits source_snapshot, build_summary and all
    artifact/crypto fields; list and polling state need none of them.
    """
    scope_ids = [scope.id for scope in scopes]
    if not scope_ids:
        return {}, {}, {}

    publication_fields = (
        "id",
        "scope_state_id",
        "version_number",
        "status",
        "build_error",
        "source_fingerprint",
        "created_at",
        "published_at",
    )
    latest_by_scope = {
        publication.scope_state_id: publication
        for publication in DatasetPublication.objects.filter(scope_state_id__in=scope_ids)
        .order_by("scope_state_id", "-version_number")
        .distinct("scope_state_id")
        .only(*publication_fields)
    }
    active_jobs = {
        job.scope_state_id: job
        for job in PublicationJob.objects.filter(
            scope_state_id__in=scope_ids,
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        ).only(
            "id",
            "scope_state_id",
            "status",
            "trigger_type",
            "not_before",
            "build_publication_id",
        )
    }
    linked_publication_ids = {
        publication_id
        for scope in scopes
        for publication_id in (
            scope.latest_built_publication_id,
            scope.current_published_publication_id,
        )
        if publication_id is not None
    }
    linked_publication_ids.update(
        job.build_publication_id
        for job in active_jobs.values()
        if job.build_publication_id is not None
    )
    linked_publications = {
        publication.id: publication
        for publication in DatasetPublication.objects.filter(pk__in=linked_publication_ids).only(
            *publication_fields
        )
    }
    return latest_by_scope, active_jobs, linked_publications


def _scope_state_inputs(
    *,
    scope: DatasetScopeState,
    latest_by_scope: dict,
    active_jobs: dict,
    linked_publications: dict,
):
    """Return the compact values shared by detailed and aggregate state paths."""
    latest = latest_by_scope.get(scope.id)
    job = active_jobs.get(scope.id)
    latest_built = linked_publications.get(scope.latest_built_publication_id)
    current_published = linked_publications.get(scope.current_published_publication_id)
    current_fingerprint = scope.current_source_fingerprint
    if current_published is None:
        dirty = True
    elif current_fingerprint and current_published.source_fingerprint:
        dirty = current_fingerprint != current_published.source_fingerprint
    else:
        # Legacy rows are initialized only through a locked service path; list
        # rendering deliberately never rebuilds canonical sources.
        dirty = True
    return latest, job, latest_built, current_published, current_fingerprint, dirty


def scope_operational_states_for_scopes(
    scopes: list[DatasetScopeState], *, now=None
) -> list[dict[str, Any]]:
    """Return detailed state rows for scopes that have already been paginated."""
    now = now or timezone.now()
    scopes = list(scopes)
    latest_by_scope, active_jobs, linked_publications = _state_publications_and_jobs(scopes)

    rows: list[dict[str, Any]] = []
    for scope in scopes:
        definition = get_dataset_definition(scope.dataset_type_code)
        latest, job, latest_built, current_published, current_fingerprint, dirty = (
            _scope_state_inputs(
                scope=scope,
                latest_by_scope=latest_by_scope,
                active_jobs=active_jobs,
                linked_publications=linked_publications,
            )
        )
        state = compute_scope_state(
            dirty=dirty,
            latest_status=latest.status if latest else None,
            latest_source_fingerprint=latest.source_fingerprint if latest else None,
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
                "latest_publication_id": latest.id if latest else None,
                "latest_publication_version": latest.version_number if latest else None,
                "current_published_publication_id": (
                    current_published.id if current_published else None
                ),
                "current_update_label": (
                    CURRENT_UPDATE_LABELS.get(state) if current_published else None
                ),
                "current_update_badge": (
                    STATE_BADGE.get(state) if current_published and state != CURRENT else None
                ),
                "build_error": latest.build_error if latest and state == FAILED else None,
                "last_activity": (latest.created_at if latest else None) or scope.updated_at,
                "active_job_status": job.status if job else None,
                "active_job_trigger_type": job.trigger_type if job else None,
                "active_job_not_before": job.not_before if job else None,
                "active_job_publication_id": job.build_publication_id if job else None,
                "active_job_publication_version": (
                    linked_publications[job.build_publication_id].version_number
                    if job and job.build_publication_id in linked_publications
                    else None
                ),
                "should_poll": state == BUILDING
                or (
                    state in (UPDATE_QUEUED, QUEUED)
                    and job is not None
                    and job.not_before is not None
                    and job.not_before <= now
                    and now - job.not_before <= STAGED_POLL_GRACE_PERIOD
                ),
            }
        )
    return rows


def scope_operational_states(
    department, *, now=None, scope_ids: set[object] | None = None
) -> list[dict[str, Any]]:
    """Return detailed state for all requested scopes (compatibility helper).

    Publications list callers should use publication_scope_queryset followed by
    scope_operational_states_for_scopes so only the visible page is enriched.
    """
    scopes = publication_scope_queryset(department)
    if scope_ids is not None:
        scopes = scopes.filter(pk__in=scope_ids)
    return scope_operational_states_for_scopes(list(scopes), now=now)


def dataset_publication_summaries(
    department, *, dataset_type_codes: set[str] | None = None, now=None
) -> dict[str, dict[str, Any]]:
    """Return compact per-dataset card state without detailed scope row models.

    Station-scoped datasets may have hundreds of scopes.  Data Hub only needs
    a module-level publication summary, so it derives aggregate counters from
    the same narrow batch projections instead of constructing every row's UI
    affordances, labels and actions.
    """
    now = now or timezone.now()
    scopes = publication_scope_queryset(department, include_station=False)
    if dataset_type_codes is not None:
        scopes = scopes.filter(dataset_type_code__in=dataset_type_codes)
    scopes = list(scopes)
    latest_by_scope, active_jobs, linked_publications = _state_publications_and_jobs(scopes)
    summaries: dict[str, dict[str, Any]] = {}
    state_priority = {
        BUILDING: 6,
        UPDATE_QUEUED: 5,
        QUEUED: 5,
        FAILED: 4,
        NEEDS_REBUILD: 3,
        READY_TO_PUBLISH: 2,
        CURRENT: 1,
        NOT_PUBLISHED: 0,
    }
    for scope in scopes:
        latest, job, latest_built, current, current_fingerprint, dirty = _scope_state_inputs(
            scope=scope,
            latest_by_scope=latest_by_scope,
            active_jobs=active_jobs,
            linked_publications=linked_publications,
        )
        state = compute_scope_state(
            dirty=dirty,
            latest_status=latest.status if latest else None,
            latest_source_fingerprint=latest.source_fingerprint if latest else None,
            current_source_fingerprint=current_fingerprint,
            latest_built_status=latest_built.status if latest_built else None,
            current_published=current is not None,
            active_job_status=job.status if job else None,
            active_job_not_before=job.not_before if job else None,
            now=now,
        )
        summary = summaries.setdefault(
            scope.dataset_type_code,
            {
                "distributed_version": None,
                "state": NOT_PUBLISHED,
                "state_label": STATE_LABELS[NOT_PUBLISHED],
                "current_update_label": None,
                "scope_count": 0,
                "published_scope_count": 0,
            },
        )
        summary["scope_count"] += 1
        if current is not None:
            summary["published_scope_count"] += 1
            # A card is an aggregate, so its version is a representative
            # current version rather than a candidate version.  It can never
            # expose a STAGED/BUILDING attempt as authoritative.
            if (
                summary["distributed_version"] is None
                or current.version_number > summary["distributed_version"]
            ):
                summary["distributed_version"] = current.version_number
        if state_priority[state] > state_priority[summary["state"]]:
            summary["state"] = state

    for summary in summaries.values():
        if summary["distributed_version"] is None:
            summary["state"] = NOT_PUBLISHED
        summary["state_label"] = STATE_LABELS[summary["state"]]
        summary["current_update_label"] = (
            CURRENT_UPDATE_LABELS.get(summary["state"])
            if summary["distributed_version"] is not None
            else None
        )
    return summaries


def operational_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate scope rows into the Status & Context card metrics."""
    summary = {key: 0 for key in STATE_LABELS}
    summary["total"] = len(rows)
    for row in rows:
        summary[row["state"]] += 1
    return summary
