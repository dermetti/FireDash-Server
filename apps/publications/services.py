from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.publications.artifacts import (
    ArtifactError,
    build_encrypted_artifact,
    remove_artifact,
    remove_artifact_path,
)
from apps.publications.builders import (
    PublicationBuildError,
    build_artifact,
    build_change_summary,
    build_summary,
    validate_built_summary,
)
from apps.publications.feature_services import FeatureDisabledError, require_feature
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    PublicationActivation,
    PublicationJob,
)
from apps.publications.registry import (
    DatasetRegistryError,
    get_dataset_definition,
    validate_dataset_scope,
)
from apps.publications.wake import wake_publication_build_worker


class PublicationError(ValueError):
    pass


def _scope_filter(*, department, station, dataset_type_code: str) -> dict[str, object]:
    return {
        "department": department,
        "station": station,
        "dataset_type_code": dataset_type_code,
    }


def _validate_scope(*, department, station, dataset_type_code: str) -> None:
    try:
        validate_dataset_scope(dataset_type_code=dataset_type_code, station=station)
    except DatasetRegistryError as error:
        raise PublicationError(str(error)) from error
    if station is not None and station.department_id != department.id:
        raise PublicationError("Station must belong to the scope department.")
    try:
        definition = get_dataset_definition(dataset_type_code)
        require_feature(department=department, feature_code=definition.feature_code)
    except (DatasetRegistryError, FeatureDisabledError) as error:
        raise PublicationError(str(error)) from error


def _locked_scope(*, department, station, dataset_type_code: str) -> DatasetScopeState:
    scope = (
        DatasetScopeState.objects.select_for_update()
        .filter(
            **_scope_filter(
                department=department, station=station, dataset_type_code=dataset_type_code
            )
        )
        .first()
    )
    if scope is None:
        raise PublicationError("Dataset scope has not been initialized.")
    return scope


@transaction.atomic
def mark_dirty(
    *, department, station=None, dataset_type_code: str, actor=None
) -> DatasetScopeState:
    """Advance a scope revision and queue exactly one build after commit."""
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    now = timezone.now()
    scope = (
        DatasetScopeState.objects.select_for_update()
        .filter(
            **_scope_filter(
                department=department, station=station, dataset_type_code=dataset_type_code
            )
        )
        .first()
    )
    if scope is None:
        scope = DatasetScopeState(
            **_scope_filter(
                department=department, station=station, dataset_type_code=dataset_type_code
            )
        )
        scope.full_clean()
        try:
            # A savepoint keeps the outer dirty-mark transaction usable after a race.
            with transaction.atomic():
                scope.save()
        except IntegrityError:
            scope = _locked_scope(
                department=department, station=station, dataset_type_code=dataset_type_code
            )
    scope.source_revision += 1
    if scope.dirty_since is None:
        scope.dirty_since = now
    scope.save(update_fields=("source_revision", "dirty_since", "updated_at"))
    record_event(
        action="publication.scope_marked_dirty",
        actor_user=actor,
        department=department,
        station=station,
        target_type="dataset_scope_state",
        target_uuid=scope.id,
        metadata={"dataset_type_code": dataset_type_code, "source_revision": scope.source_revision},
    )
    transaction.on_commit(
        lambda: enqueue_publication_job(
            department=department,
            station=station,
            dataset_type_code=dataset_type_code,
            requested_by=actor,
            trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        )
    )
    return scope


@transaction.atomic
def enqueue_publication_job(
    *,
    department,
    station=None,
    dataset_type_code: str,
    requested_by=None,
    trigger_type: str = PublicationJob.TriggerType.DATA_CHANGE,
    debounce_started_at: datetime | None = None,
) -> PublicationJob | None:
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    scope = _locked_scope(
        department=department, station=station, dataset_type_code=dataset_type_code
    )
    now = timezone.now()
    active = (
        PublicationJob.objects.select_for_update()
        .filter(
            **_scope_filter(
                department=department, station=station, dataset_type_code=dataset_type_code
            ),
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        )
        .first()
    )
    if active is not None:
        if active.status == PublicationJob.Status.RUNNING:
            return None
        # Coalesce an existing PENDING job to the latest source revision.
        active.source_revision = scope.source_revision
        if trigger_type in (
            PublicationJob.TriggerType.USER_REQUEST,
            PublicationJob.TriggerType.BULK_REQUEST,
        ):
            # An explicit rebuild makes any pending work immediately eligible.
            active.trigger_type = trigger_type
            active.requested_by = requested_by
            active.not_before = None
        elif active.trigger_type == PublicationJob.TriggerType.DATA_CHANGE:
            active.not_before = _data_change_not_before(
                now=now, debounce_started_at=active.debounce_started_at
            )
        active.save(update_fields=("source_revision", "trigger_type", "requested_by", "not_before"))
        return active

    job = PublicationJob(
        **_scope_filter(
            department=department, station=station, dataset_type_code=dataset_type_code
        ),
        source_revision=scope.source_revision,
        scope_state=scope,
        requested_by=requested_by,
        trigger_type=trigger_type,
    )
    if trigger_type == PublicationJob.TriggerType.DATA_CHANGE:
        job.debounce_started_at = debounce_started_at or now
        job.not_before = _data_change_not_before(
            now=now, debounce_started_at=job.debounce_started_at
        )
    else:
        job.debounce_started_at = None
        job.not_before = None
    job.full_clean()
    try:
        job.save()
    except IntegrityError:
        return None
    record_event(
        action="publication.job_queued",
        actor_user=requested_by,
        department=department,
        station=station,
        target_type="publication_job",
        target_uuid=job.id,
        metadata={"dataset_type_code": dataset_type_code, "source_revision": job.source_revision},
    )
    return job


def _data_change_not_before(*, now: datetime, debounce_started_at: datetime | None) -> datetime:
    """Schedule coalesced source changes for the next nightly build window."""
    nightly = now.replace(hour=0, minute=5, second=0, microsecond=0)
    if now.time() >= time(hour=0, minute=5):
        nightly += timedelta(days=1)
    return nightly


@transaction.atomic
def claim_next_job() -> PublicationJob | None:
    now = timezone.now()
    job = (
        PublicationJob.objects.select_for_update(skip_locked=True)
        .filter(status=PublicationJob.Status.PENDING)
        .filter(Q(not_before__isnull=True) | Q(not_before__lte=now))
        .order_by("created_at")
        .first()
    )
    if job is None:
        return None
    scope = DatasetScopeState.objects.select_for_update().get(pk=job.scope_state_id)
    try:
        definition = get_dataset_definition(job.dataset_type_code)
    except DatasetRegistryError:
        job.status = PublicationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error_category = "registry"
        job.error_message = "Unknown dataset type code."
        job.save(update_fields=("status", "completed_at", "error_category", "error_message"))
        return job
    now = timezone.now()
    job.source_revision = scope.source_revision
    # The locked scope serializes allocation. Every build attempt consumes an
    # immutable number, including attempts that later fail or become obsolete.
    version_number = (
        DatasetPublication.objects.filter(scope_state=scope).aggregate(
            maximum=Max("version_number")
        )["maximum"]
        or 0
    ) + 1
    publication = DatasetPublication.objects.create(
        department=job.department,
        station=job.station,
        dataset_type_code=job.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
        schema_version=definition.current_schema_version,
        source_revision=job.source_revision,
        status=DatasetPublication.Status.BUILDING,
        created_by=job.requested_by,
    )
    job.status = PublicationJob.Status.RUNNING
    job.started_at = now
    job.heartbeat_at = now
    job.error_category = ""
    job.error_message = ""
    job.attempt_count += 1
    job.build_publication = publication
    job.save(
        update_fields=(
            "status",
            "started_at",
            "heartbeat_at",
            "source_revision",
            "attempt_count",
            "build_publication",
            "error_category",
            "error_message",
        )
    )
    return job


@transaction.atomic
def heartbeat_job(*, job_id) -> bool:
    job = PublicationJob.objects.select_for_update().filter(pk=job_id).first()
    if job is None or job.status != PublicationJob.Status.RUNNING:
        return False
    job.heartbeat_at = timezone.now()
    job.save(update_fields=("heartbeat_at",))
    return True


def process_next_job() -> PublicationJob | None:
    job = claim_next_job()
    if job is None:
        return None
    return build_claimed_job(job_id=job.id)


def build_claimed_job(*, job_id) -> PublicationJob:
    """Build a bounded summary outside locks, then atomically finalize it."""
    job = PublicationJob.objects.select_related("department", "station").get(pk=job_id)
    if job.status != PublicationJob.Status.RUNNING:
        return job
    if job.build_publication_id is None:
        return fail_publication_job(
            job_id=job.id,
            error_category="worker",
            error_message="Claim did not allocate a publication.",
        )
    artifact: dict[str, object] | None = None
    try:
        definition = get_dataset_definition(job.dataset_type_code)
        _validate_scope(
            department=job.department, station=job.station, dataset_type_code=job.dataset_type_code
        )
        summary = build_summary(
            definition=definition,
            department=job.department,
            station=job.station,
            source_revision=job.source_revision,
        )
        publication = DatasetPublication.objects.get(pk=job.build_publication_id)
        artifact = build_encrypted_artifact(
            publication=publication,
            plaintext=build_artifact(
                definition=definition,
                department=job.department,
                station=job.station,
                source_revision=job.source_revision,
            ),
        )
        return finalize_publication_job(job_id=job.id, summary=summary, artifact=artifact)
    except (DatasetRegistryError, PublicationBuildError, PublicationError, ArtifactError) as error:
        _compensate_artifact(artifact)
        return fail_publication_job(job_id=job.id, error_message=str(error))
    except ValidationError:
        _compensate_artifact(artifact)
        return fail_publication_job(
            job_id=job.id,
            error_message="Publication metadata is invalid.",
            error_category="validation",
        )
    except IntegrityError as error:
        # Only the publication closeout triggers (SQLSTATE P0001: the canonical
        # artifact-path and artifact-metadata guards) are known, per-publication
        # integrity failures. Those become FAILED with artifact compensation so
        # later independent jobs still run. Any other IntegrityError (unique
        # violation, check constraint, not-null, etc.) signals a broken
        # application invariant or schema bug and must propagate to systemd.
        if not _is_publication_guard_error(error):
            raise
        _compensate_artifact(artifact)
        return fail_publication_job(
            job_id=job.id,
            error_message="Publication could not be finalized.",
            error_category="finalization",
        )


def _is_publication_guard_error(error: IntegrityError) -> bool:
    """Return True for the publication closeout trigger guards (SQLSTATE P0001)."""
    cause = error.__cause__
    return getattr(cause, "sqlstate", None) == "P0001"


def _compensate_artifact(artifact: dict[str, object] | None) -> None:
    """Remove a promoted artifact that could not be finalized (best effort)."""
    if artifact is None:
        return
    try:
        remove_artifact_path(artifact["artifact_path"])
    except ArtifactError:
        pass


def _queue_follow_up_if_stale(*, job: PublicationJob) -> None:
    """Queue a debounced DATA_CHANGE follow-up if the scope moved past this job.

    When a RUNNING job leaves active state and the scope has since advanced to a
    newer source revision, the newer revision must not be stranded without a
    follow-up build. Reuse the scope's existing dirty-window start so the
    follow-up honours the original trailing-debounce/maximum-deferral timing
    rather than starting a fresh, independent window.
    """
    scope = _locked_scope(
        department=job.department, station=job.station, dataset_type_code=job.dataset_type_code
    )
    if scope.source_revision <= job.source_revision:
        return
    department = job.department
    station = job.station
    dataset_type_code = job.dataset_type_code
    debounce_started_at = scope.dirty_since
    transaction.on_commit(
        lambda: enqueue_publication_job(
            department=department,
            station=station,
            dataset_type_code=dataset_type_code,
            trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
            debounce_started_at=debounce_started_at,
        )
    )


@transaction.atomic
def finalize_publication_job(
    *, job_id, summary: dict[str, object], artifact: dict[str, object]
) -> PublicationJob:
    job = (
        PublicationJob.objects.select_for_update(of=("self",))
        .select_related("department", "station")
        .get(pk=job_id)
    )
    if job.status != PublicationJob.Status.RUNNING:
        return job
    if job.build_publication_id is None:
        return fail_publication_job(
            job_id=job.id,
            error_category="worker",
            error_message="Claim did not allocate a publication.",
        )
    scope = _locked_scope(
        department=job.department, station=job.station, dataset_type_code=job.dataset_type_code
    )
    now = timezone.now()
    publication = DatasetPublication.objects.select_for_update().get(pk=job.build_publication_id)
    if scope.source_revision != job.source_revision:
        remove_artifact_path(artifact["artifact_path"])
        publication.status = DatasetPublication.Status.OBSOLETE
        # Keep the assigned value: it is included in the signed artifact
        # canonical payload and terminal history must remain verifiable.
        publication.save(update_fields=("status",))
        job.status = PublicationJob.Status.OBSOLETE
        job.completed_at = now
        job.save(update_fields=("status", "completed_at"))
        record_event(
            action="publication.job_obsolete",
            department=job.department,
            station=job.station,
            target_type="publication_job",
            target_uuid=job.id,
            metadata={
                "dataset_type_code": job.dataset_type_code,
                "source_revision": job.source_revision,
            },
        )
        transaction.on_commit(
            lambda: enqueue_publication_job(
                department=job.department,
                station=job.station,
                dataset_type_code=job.dataset_type_code,
                trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
            )
        )
        return job
    definition = get_dataset_definition(job.dataset_type_code)
    validate_built_summary(definition=definition, summary=summary)
    publication.build_summary = summary
    previous_publication = scope.latest_built_publication
    previous_summary = previous_publication.build_summary if previous_publication else {}
    publication.change_summary = build_change_summary(
        definition=definition, previous=previous_summary, current=summary
    )
    for field, value in artifact.items():
        setattr(publication, field, value)
    publication.artifact_status = DatasetPublication.ArtifactStatus.READY
    publication.artifact_ready = True
    previous_current = scope.current_published_publication
    if previous_current is not None:
        previous_current.status = DatasetPublication.Status.SUPERSEDED
        previous_current.save(update_fields=("status",))
    publication.status = DatasetPublication.Status.PUBLISHED
    publication.published_at = now
    publication.supersedes = previous_current
    publication.full_clean()
    publication.save(
        update_fields=(
            "build_summary",
            "change_summary",
            "artifact_status",
            "artifact_ready",
            *artifact.keys(),
            "status",
            "published_at",
            "supersedes",
        )
    )
    scope.latest_built_publication = publication
    scope.current_published_publication = publication
    scope.dirty_since = None
    scope.save(
        update_fields=(
            "latest_built_publication",
            "current_published_publication",
            "dirty_since",
            "updated_at",
        )
    )
    job.status = PublicationJob.Status.SUCCEEDED
    job.completed_at = now
    job.heartbeat_at = now
    job.save(update_fields=("status", "completed_at", "heartbeat_at"))
    record_event(
        action="publication.published",
        department=job.department,
        station=job.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={
            "dataset_type_code": job.dataset_type_code,
            "version_number": publication.version_number,
            "origin": job.trigger_type.lower(),
            "source_revision": publication.source_revision,
        },
    )
    return job


@transaction.atomic
def fail_publication_job(
    *, job_id, error_message: str, error_category: str = "build"
) -> PublicationJob:
    job = PublicationJob.objects.select_for_update().get(pk=job_id)
    if job.status != PublicationJob.Status.RUNNING:
        return job
    job.status = PublicationJob.Status.FAILED
    job.completed_at = timezone.now()
    job.error_message = error_message[:2000]
    job.error_category = error_category[:32]
    job.save(update_fields=("status", "completed_at", "error_message", "error_category"))
    if job.build_publication_id is not None:
        publication = DatasetPublication.objects.filter(pk=job.build_publication_id).first()
        if publication is not None:
            remove_artifact(publication)
        DatasetPublication.objects.filter(
            pk=job.build_publication_id, status=DatasetPublication.Status.BUILDING
        ).update(
            status=DatasetPublication.Status.FAILED,
            artifact_status=DatasetPublication.ArtifactStatus.FAILED,
            build_error=job.error_message,
        )
    record_event(
        action="publication.build_failed",
        department=job.department,
        station=job.station,
        target_type="publication_job",
        target_uuid=job.id,
        metadata={"dataset_type_code": job.dataset_type_code},
    )
    _queue_follow_up_if_stale(job=job)
    return job


@transaction.atomic
def recover_stale_jobs(*, timeout: timedelta, max_attempts: int = 3) -> int:
    if timeout <= timedelta(0):
        raise PublicationError("Job recovery timeout must be positive.")
    if max_attempts < 1:
        raise PublicationError("Maximum job attempts must be positive.")
    cutoff = timezone.now() - timeout
    jobs = list(
        PublicationJob.objects.select_for_update(skip_locked=True).filter(
            status=PublicationJob.Status.RUNNING,
            heartbeat_at__lt=cutoff,
        )
    )
    for job in jobs:
        if job.build_publication_id is not None:
            DatasetPublication.objects.filter(
                pk=job.build_publication_id, status=DatasetPublication.Status.BUILDING
            ).update(
                status=DatasetPublication.Status.FAILED,
                build_error="Worker heartbeat timed out.",
            )
        if job.attempt_count >= max_attempts:
            job.status = PublicationJob.Status.FAILED
            job.completed_at = timezone.now()
            job.error_category = "retry_exhausted"
            job.error_message = "Publication build exceeded the maximum retry attempts."
            job.save(update_fields=("status", "completed_at", "error_category", "error_message"))
            _queue_follow_up_if_stale(job=job)
        else:
            job.status = PublicationJob.Status.PENDING
            job.started_at = None
            job.heartbeat_at = None
            job.build_publication = None
            job.error_category = "retryable_timeout"
            job.error_message = "Worker heartbeat timed out; the build will be retried."
            job.save(
                update_fields=(
                    "status",
                    "started_at",
                    "heartbeat_at",
                    "build_publication",
                    "error_category",
                    "error_message",
                )
            )
    return len(jobs)


@transaction.atomic
def publish_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    publication = (
        DatasetPublication.objects.select_for_update(of=("self",))
        .select_related("department", "station")
        .get(pk=publication.pk)
    )
    require_department_admin(actor, publication.department)
    if publication.status != DatasetPublication.Status.READY_FOR_REVIEW:
        raise PublicationError("Only a review-ready publication can be published.")
    _validate_scope(
        department=publication.department,
        station=publication.station,
        dataset_type_code=publication.dataset_type_code,
    )
    if publication.artifact_status != DatasetPublication.ArtifactStatus.READY:
        raise PublicationError("Publication artifact is not ready.")
    scope = _locked_scope(
        department=publication.department,
        station=publication.station,
        dataset_type_code=publication.dataset_type_code,
    )
    previous = scope.current_published_publication
    if previous is not None:
        previous.status = DatasetPublication.Status.SUPERSEDED
        previous.save(update_fields=("status",))
    now = timezone.now()
    publication.status = DatasetPublication.Status.PUBLISHED
    publication.published_at = now
    publication.published_by = actor
    publication.supersedes = previous
    publication.save(update_fields=("status", "published_at", "published_by", "supersedes"))
    scope.current_published_publication = publication
    scope.save(update_fields=("current_published_publication", "updated_at"))
    PublicationActivation.objects.create(
        publication=publication,
        scope_state=scope,
        previous_publication=previous,
        action=PublicationActivation.Action.PUBLISH,
        activated_by=actor,
    )
    record_event(
        action="publication.published",
        actor_user=actor,
        department=publication.department,
        station=publication.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={"dataset_type_code": publication.dataset_type_code},
    )
    return publication


@transaction.atomic
def reject_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    publication = (
        DatasetPublication.objects.select_for_update(of=("self",))
        .select_related("department", "station")
        .get(pk=publication.pk)
    )
    require_department_admin(actor, publication.department)
    if publication.status != DatasetPublication.Status.READY_FOR_REVIEW:
        raise PublicationError("Only a review-ready publication can be rejected.")
    _validate_scope(
        department=publication.department,
        station=publication.station,
        dataset_type_code=publication.dataset_type_code,
    )
    publication.status = DatasetPublication.Status.REJECTED
    publication.save(update_fields=("status",))
    record_event(
        action="publication.rejected",
        actor_user=actor,
        department=publication.department,
        station=publication.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={"dataset_type_code": publication.dataset_type_code},
    )
    return publication


@transaction.atomic
def request_rebuild(
    *, actor, department, station=None, dataset_type_code: str
) -> DatasetScopeState:
    require_department_admin(actor, department)
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    with transaction.atomic():
        scope = (
            DatasetScopeState.objects.select_for_update()
            .filter(
                **_scope_filter(
                    department=department, station=station, dataset_type_code=dataset_type_code
                )
            )
            .first()
        )
        if scope is None:
            scope = DatasetScopeState(
                **_scope_filter(
                    department=department, station=station, dataset_type_code=dataset_type_code
                )
            )
            scope.full_clean()
            scope.save()
        transaction.on_commit(
            lambda: enqueue_publication_job(
                department=department,
                station=station,
                dataset_type_code=dataset_type_code,
                requested_by=actor,
                trigger_type=PublicationJob.TriggerType.USER_REQUEST,
            )
        )
        transaction.on_commit(wake_publication_build_worker)
        record_event(
            action="publication.rebuild_requested",
            actor_user=actor,
            department=department,
            station=station,
            target_type="dataset_scope_state",
            target_uuid=scope.id,
            metadata={
                "dataset_type_code": dataset_type_code,
                "source_revision": scope.source_revision,
            },
        )
        return scope


@transaction.atomic
def rollback_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    publication = (
        DatasetPublication.objects.select_for_update(of=("self",))
        .select_related("department", "station")
        .get(pk=publication.pk)
    )
    require_department_admin(actor, publication.department)
    if publication.status != DatasetPublication.Status.SUPERSEDED:
        raise PublicationError("Only a superseded publication can be restored.")
    _validate_scope(
        department=publication.department,
        station=publication.station,
        dataset_type_code=publication.dataset_type_code,
    )
    scope = _locked_scope(
        department=publication.department,
        station=publication.station,
        dataset_type_code=publication.dataset_type_code,
    )
    previous = scope.current_published_publication
    if previous is None:
        raise PublicationError("Dataset scope has no current publication.")
    previous.status = DatasetPublication.Status.SUPERSEDED
    previous.save(update_fields=("status",))
    publication.status = DatasetPublication.Status.PUBLISHED
    publication.published_at = timezone.now()
    publication.published_by = actor
    publication.save(update_fields=("status", "published_at", "published_by"))
    scope.current_published_publication = publication
    scope.save(update_fields=("current_published_publication", "updated_at"))
    PublicationActivation.objects.create(
        publication=publication,
        scope_state=scope,
        previous_publication=previous,
        action=PublicationActivation.Action.ROLLBACK,
        activated_by=actor,
    )
    record_event(
        action="publication.rolled_back",
        actor_user=actor,
        department=publication.department,
        station=publication.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={"dataset_type_code": publication.dataset_type_code},
    )
    return publication


def _latest_publication_status_by_scope(department) -> dict[object, object]:
    """Map each scope id to the status of its most recent publication attempt."""
    latest = DatasetPublication.objects.filter(scope_state=OuterRef("pk")).order_by(
        "-version_number"
    )
    rows = (
        DatasetScopeState.objects.filter(department=department)
        .annotate(latest_status=Subquery(latest.values("status")[:1]))
        .values("id", "latest_status")
    )
    return {row["id"]: row["latest_status"] for row in rows}


@transaction.atomic
def bulk_request_rebuilds(*, actor, department) -> dict[str, int]:
    """Promote or create one immediate BULK_REQUEST intent per affected scope.

    Qualifying scopes: dirty, never successfully built, or whose most recent
    attempt failed without a later successful build. Scopes with an active
    PENDING/RUNNING job are already queued and are left untouched.
    """
    require_department_admin(actor, department)
    scopes = list(
        DatasetScopeState.objects.filter(department=department).select_related(
            "station", "latest_built_publication"
        )
    )
    latest_status = _latest_publication_status_by_scope(department)
    running_scope_ids = set(
        PublicationJob.objects.filter(
            department=department,
            status=PublicationJob.Status.RUNNING,
        ).values_list("scope_state_id", flat=True)
    )

    created = promoted = already_running = skipped_current = 0
    for scope in scopes:
        if scope.id in running_scope_ids:
            already_running += 1
            continue
        needs_attention = (
            scope.dirty_since is not None
            or scope.latest_built_publication_id is None
            or latest_status.get(scope.id) == DatasetPublication.Status.FAILED
        )
        if not needs_attention:
            skipped_current += 1
            continue
        before = PublicationJob.objects.filter(
            scope_state=scope, status=PublicationJob.Status.PENDING
        ).exists()
        queued = enqueue_publication_job(
            department=department,
            station=scope.station,
            dataset_type_code=scope.dataset_type_code,
            requested_by=actor,
            trigger_type=PublicationJob.TriggerType.BULK_REQUEST,
        )
        if queued is not None:
            if before:
                promoted += 1
            else:
                created += 1

    if created or promoted:
        transaction.on_commit(wake_publication_build_worker)

    record_event(
        action="publication.bulk_rebuild_requested",
        actor_user=actor,
        department=department,
        target_type="dataset_scope_state",
        metadata={
            "created": created,
            "promoted": promoted,
            "already_running": already_running,
            "skipped_current": skipped_current,
        },
    )
    return {
        "requested": created + promoted,
        "already_queued": promoted,
        "already_current": skipped_current,
        "created": created,
        "promoted": promoted,
        "already_running": already_running,
        "skipped_current": skipped_current,
    }
