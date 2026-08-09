from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Max
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
) -> PublicationJob | None:
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    scope = _locked_scope(
        department=department, station=station, dataset_type_code=dataset_type_code
    )
    if PublicationJob.objects.filter(
        **_scope_filter(
            department=department, station=station, dataset_type_code=dataset_type_code
        ),
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    ).exists():
        return None
    job = PublicationJob(
        **_scope_filter(
            department=department, station=station, dataset_type_code=dataset_type_code
        ),
        source_revision=scope.source_revision,
        scope_state=scope,
        requested_by=requested_by,
        trigger_type=trigger_type,
    )
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


@transaction.atomic
def claim_next_job() -> PublicationJob | None:
    job = (
        PublicationJob.objects.select_for_update(skip_locked=True)
        .filter(status=PublicationJob.Status.PENDING)
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
    except (DatasetRegistryError, PublicationBuildError, PublicationError, ArtifactError) as error:
        return fail_publication_job(job_id=job.id, error_message=str(error))
    return finalize_publication_job(job_id=job.id, summary=summary, artifact=artifact)


@transaction.atomic
def finalize_publication_job(
    *, job_id, summary: dict[str, object], artifact: dict[str, object]
) -> PublicationJob:
    job = (
        PublicationJob.objects.select_for_update()
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
    publication.status = DatasetPublication.Status.READY_FOR_REVIEW
    publication.full_clean()
    publication.save(
        update_fields=(
            "build_summary",
            "change_summary",
            "artifact_status",
            "artifact_ready",
            *artifact.keys(),
            "status",
        )
    )
    scope.latest_built_publication = publication
    scope.dirty_since = None
    scope.save(update_fields=("latest_built_publication", "dirty_since", "updated_at"))
    job.status = PublicationJob.Status.SUCCEEDED
    job.completed_at = now
    job.heartbeat_at = now
    job.save(update_fields=("status", "completed_at", "heartbeat_at"))
    record_event(
        action="publication.build_succeeded",
        department=job.department,
        station=job.station,
        target_type="dataset_publication",
        target_uuid=publication.id,
        metadata={
            "dataset_type_code": job.dataset_type_code,
            "version_number": publication.version_number,
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
                status=DatasetPublication.Status.FAILED, build_error="Worker heartbeat timed out."
            )
        if job.attempt_count >= max_attempts:
            job.status = PublicationJob.Status.FAILED
            job.completed_at = timezone.now()
            job.error_category = "retry_exhausted"
            job.error_message = "Publication build exceeded the maximum retry attempts."
            job.save(update_fields=("status", "completed_at", "error_category", "error_message"))
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
        DatasetPublication.objects.select_for_update()
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
        DatasetPublication.objects.select_for_update()
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
        scope = _locked_scope(
            department=department, station=station, dataset_type_code=dataset_type_code
        )
        transaction.on_commit(
            lambda: enqueue_publication_job(
                department=department,
                station=station,
                dataset_type_code=dataset_type_code,
                requested_by=actor,
                trigger_type=PublicationJob.TriggerType.USER_REQUEST,
            )
        )
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
        DatasetPublication.objects.select_for_update()
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
