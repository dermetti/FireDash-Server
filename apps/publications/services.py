import logging
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
    remove_artifact_path,
)
from apps.publications.builders import (
    PublicationBuildError,
    build_artifact,
    build_change_summary,
    build_source_payload,
    build_summary,
    source_fingerprint_for_payload,
    validate_built_summary,
)
from apps.publications.document_artifacts import release_terminal_document_artifact_references
from apps.publications.manifests import revoke_publication_dataset_key_grants
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


logger = logging.getLogger(__name__)


def _schedule_artifact_removal(relative_path: str) -> None:
    """Remove retired ciphertext only after its lifecycle transaction commits."""
    if not relative_path:
        return

    def remove_after_commit() -> None:
        try:
            remove_artifact_path(relative_path)
        except (ArtifactError, OSError) as error:
            # The database transition remains authoritative. Existing orphan
            # artifact maintenance can safely retry a failed filesystem cleanup.
            logger.warning("Publication artifact cleanup deferred for %s: %s", relative_path, error)

    transaction.on_commit(remove_after_commit)


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


def _allocate_staged_publication(
    *,
    scope: DatasetScopeState,
    job: PublicationJob,
    source_fingerprint_value: str,
    source_snapshot: dict[str, object],
) -> DatasetPublication:
    """Allocate an immutable attempt while the canonical scope row is locked."""
    definition = get_dataset_definition(job.dataset_type_code)
    version_number = (
        DatasetPublication.objects.filter(scope_state=scope).aggregate(
            maximum=Max("version_number")
        )["maximum"]
        or 0
    ) + 1
    schema_version = (
        2
        if scope.dataset_type_code == "department_fire_plans"
        and scope.delivery_format == DatasetScopeState.DeliveryFormat.DOCUMENT_MANIFEST_V2
        else definition.current_schema_version
    )
    return DatasetPublication.objects.create(
        department=job.department,
        station=job.station,
        dataset_type_code=job.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
        schema_version=schema_version,
        source_revision=job.source_revision,
        source_fingerprint=source_fingerprint_value,
        source_snapshot=source_snapshot,
        status=DatasetPublication.Status.STAGED,
        created_by=job.requested_by,
    )


@transaction.atomic
def cut_over_fire_plan_scope_to_document_manifest(
    *, actor, scope: DatasetScopeState
) -> DatasetScopeState:
    """Explicitly select v2 delivery and queue its first normal build."""
    require_department_admin(actor, scope.department)
    locked = _locked_scope(
        department=scope.department,
        station=scope.station,
        dataset_type_code=scope.dataset_type_code,
    )
    if locked.dataset_type_code != "department_fire_plans" or locked.station_id is not None:
        raise PublicationError(
            "Only department Fire Plan scopes support document-manifest cutover."
        )
    if locked.delivery_format == DatasetScopeState.DeliveryFormat.DOCUMENT_MANIFEST_V2:
        current = locked.current_published_publication
        if current is not None and current.schema_version == 2:
            return locked
        if PublicationJob.objects.select_for_update().filter(
            scope_state=locked,
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        ).exists():
            return locked
    if PublicationJob.objects.select_for_update().filter(
        scope_state=locked,
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    ).exists():
        raise PublicationError("A publication update is already staged or building.")
    if locked.delivery_format != DatasetScopeState.DeliveryFormat.DOCUMENT_MANIFEST_V2:
        locked.delivery_format = DatasetScopeState.DeliveryFormat.DOCUMENT_MANIFEST_V2
        locked.save(update_fields=("delivery_format", "updated_at"))
    transaction.on_commit(
        lambda: enqueue_publication_job(
            department=locked.department,
            station=locked.station,
            dataset_type_code=locked.dataset_type_code,
            requested_by=actor,
            trigger_type=PublicationJob.TriggerType.USER_REQUEST,
            allow_clean_rebuild=True,
        )
    )
    transaction.on_commit(wake_publication_build_worker)
    record_event(
        action="publication.fire_plan_document_manifest_cutover_requested",
        actor_user=actor,
        department=locked.department,
        target_type="dataset_scope_state",
        target_uuid=locked.id,
        metadata={"dataset_type_code": locked.dataset_type_code},
    )
    return locked


def _current_source_snapshot(*, scope: DatasetScopeState) -> dict[str, object]:
    return build_source_payload(
        definition=get_dataset_definition(scope.dataset_type_code),
        department=scope.department,
        station=scope.station,
    )


def _current_source_state(*, scope: DatasetScopeState) -> tuple[dict[str, object], str]:
    snapshot = _current_source_snapshot(scope=scope)
    return snapshot, source_fingerprint_for_payload(snapshot)


def _scope_is_dirty(*, scope: DatasetScopeState, fingerprint: str | None = None) -> bool:
    """Compare stored logical source fingerprints without rebuilding canonical data."""
    current = scope.current_published_publication
    if current is None:
        return True
    current_fingerprint = fingerprint or scope.current_source_fingerprint
    # Existing scopes remain conservatively dirty until a locked mutation or
    # publication operation computes the authoritative canonical fingerprint.
    if not current_fingerprint or not current.source_fingerprint:
        return True
    return current_fingerprint != current.source_fingerprint


def _newer_live_attempt_exists(*, scope: DatasetScopeState, version_number: int) -> bool:
    return DatasetPublication.objects.filter(
        scope_state=scope,
        version_number__gt=version_number,
        status__in=(DatasetPublication.Status.STAGED, DatasetPublication.Status.BUILDING),
    ).exists()


def _eligible_predecessor(
    *, scope: DatasetScopeState, before_version: int
) -> DatasetPublication | None:
    return (
        DatasetPublication.objects.select_for_update()
        .filter(
            scope_state=scope,
            status=DatasetPublication.Status.SUPERSEDED,
            version_number__lt=before_version,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
            artifact_ready=True,
        )
        .order_by("-version_number")
        .first()
    )


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
    snapshot, fingerprint = _current_source_state(scope=scope)
    scope.current_source_fingerprint = fingerprint
    dirty = _scope_is_dirty(scope=scope, fingerprint=fingerprint)
    if dirty and scope.dirty_since is None:
        scope.dirty_since = now
    if not dirty:
        scope.dirty_since = None
        # A canonical revert can make a queued automatic attempt redundant.
        # Keep its immutable version row, but prevent it from later publishing
        # an identical copy of the active source.
        # `build_publication` is nullable.  Lock the job row first and lock its
        # attempt separately below: combining select_for_update() with
        # select_related("build_publication") would issue FOR UPDATE across a
        # nullable outer join on PostgreSQL.
        pending = (
            PublicationJob.objects.select_for_update()
            .filter(scope_state=scope, status=PublicationJob.Status.PENDING)
            .first()
        )
        if pending is not None:
            pending_publication = None
            if pending.build_publication_id is not None:
                pending_publication = (
                    DatasetPublication.objects.select_for_update()
                    .filter(pk=pending.build_publication_id)
                    .first()
                )
            if (
                pending_publication is not None
                and pending_publication.status == DatasetPublication.Status.STAGED
            ):
                pending_publication.status = DatasetPublication.Status.CANCELLED
                pending_publication.build_error = (
                    "Source reverted to the active publication; staged candidate "
                    "is no longer required."
                )
                pending_publication.save(update_fields=("status", "build_error"))
            pending.status = PublicationJob.Status.CANCELLED
            pending.completed_at = now
            pending.error_category = "source_reverted"
            pending.error_message = (
                "Source reverted to the active publication; staged candidate was cancelled."
            )
            pending.save(
                update_fields=("status", "completed_at", "error_category", "error_message")
            )
    scope.save(
        update_fields=(
            "source_revision",
            "current_source_fingerprint",
            "dirty_since",
            "updated_at",
        )
    )
    record_event(
        action="publication.scope_marked_dirty",
        actor_user=actor,
        department=department,
        station=station,
        target_type="dataset_scope_state",
        target_uuid=scope.id,
        metadata={"dataset_type_code": dataset_type_code, "source_revision": scope.source_revision},
    )
    if dirty:
        # This nested service call shares the already-held scope lock and the
        # canonical mutation transaction. Passing the computed state avoids a
        # second full source reconstruction while retaining rollback safety.
        enqueue_publication_job(
            department=department,
            station=station,
            dataset_type_code=dataset_type_code,
            requested_by=actor,
            trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
            source_snapshot=snapshot,
            source_fingerprint_value=fingerprint,
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
    allow_clean_rebuild: bool = False,
    source_snapshot: dict[str, object] | None = None,
    source_fingerprint_value: str | None = None,
) -> PublicationJob | None:
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    scope = _locked_scope(
        department=department, station=station, dataset_type_code=dataset_type_code
    )
    if source_snapshot is None or source_fingerprint_value is None:
        snapshot, fingerprint = _current_source_state(scope=scope)
        if scope.current_source_fingerprint != fingerprint:
            scope.current_source_fingerprint = fingerprint
            scope.save(update_fields=("current_source_fingerprint", "updated_at"))
    else:
        snapshot = source_snapshot
        fingerprint = source_fingerprint_value
    if not allow_clean_rebuild and not _scope_is_dirty(scope=scope, fingerprint=fingerprint):
        return None
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
            active.not_before = now
        elif active.trigger_type == PublicationJob.TriggerType.DATA_CHANGE:
            active.not_before = _data_change_not_before(
                now=now, debounce_started_at=active.debounce_started_at
            )
        active.save(update_fields=("source_revision", "trigger_type", "requested_by", "not_before"))
        # A pending job may coalesce newer source data before its attempt
        # starts. Keep its staged record aligned with that final build input;
        # the immutable attempt/version identity itself is unchanged.
        if active.build_publication_id:
            staged = (
                DatasetPublication.objects.select_for_update()
                .filter(
                    pk=active.build_publication_id,
                    status=DatasetPublication.Status.STAGED,
                )
                .first()
            )
            if staged is not None:
                staged.source_revision = active.source_revision
                staged.source_fingerprint = fingerprint
                staged.source_snapshot = snapshot
                staged.save(
                    update_fields=("source_revision", "source_fingerprint", "source_snapshot")
                )
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
        job.not_before = now
    job.full_clean()
    try:
        job.save()
    except IntegrityError:
        return None
    # The pending job is an explicit staged attempt, not merely a queue entry.
    # The scope lock held above serializes this MAX()+1 allocation, and terminal
    # rows are never deleted, so version numbers remain permanently consumed.
    publication = _allocate_staged_publication(
        scope=scope,
        job=job,
        source_fingerprint_value=fingerprint,
        source_snapshot=snapshot,
    )
    job.build_publication = publication
    job.save(update_fields=("build_publication",))
    record_event(
        action="publication.job_queued",
        actor_user=requested_by,
        department=department,
        station=station,
        target_type="publication_job",
        target_uuid=job.id,
        metadata={
            "dataset_type_code": dataset_type_code,
            "source_revision": job.source_revision,
            "version_number": publication.version_number,
        },
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
    # All multi-row publication transitions lock scope -> job -> publication.
    # Pick a candidate without a row lock first, then acquire the canonical
    # scope lock before locking the job. A competing worker can harmlessly find
    # that the job is no longer pending after it obtains the same scope lock.
    candidate = (
        PublicationJob.objects.filter(status=PublicationJob.Status.PENDING)
        .filter(Q(not_before__isnull=True) | Q(not_before__lte=now))
        .order_by("created_at")
        .first()
    )
    if candidate is None:
        return None
    scope = DatasetScopeState.objects.select_for_update().get(pk=candidate.scope_state_id)
    job = (
        PublicationJob.objects.select_for_update()
        .filter(pk=candidate.pk, status=PublicationJob.Status.PENDING)
        .first()
    )
    if job is None:
        return None
    try:
        get_dataset_definition(job.dataset_type_code)
    except DatasetRegistryError:
        job.status = PublicationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error_category = "registry"
        job.error_message = "Unknown dataset type code."
        job.save(update_fields=("status", "completed_at", "error_category", "error_message"))
        return job
    now = timezone.now()
    job.source_revision = scope.source_revision
    # The scope lock makes one current source read sufficient for both a
    # legacy staged-attempt allocation and the STAGED -> BUILDING freeze.
    snapshot, fingerprint = _current_source_state(scope=scope)
    if scope.current_source_fingerprint != fingerprint:
        scope.current_source_fingerprint = fingerprint
        scope.save(update_fields=("current_source_fingerprint", "updated_at"))
    # Legacy pending jobs created before Phase 4A have no staged attempt. Give
    # them one under the same locked scope; normal jobs already own their row.
    publication = (
        DatasetPublication.objects.select_for_update().filter(pk=job.build_publication_id).first()
        if job.build_publication_id
        else _allocate_staged_publication(
            scope=scope,
            job=job,
            source_fingerprint_value=fingerprint,
            source_snapshot=snapshot,
        )
    )
    if publication is None or publication.status != DatasetPublication.Status.STAGED:
        job.status = PublicationJob.Status.OBSOLETE
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "completed_at"))
        return job
    # The scope lock makes this the mutable-candidate freeze boundary. A
    # canonical edit that committed first has already refreshed this STAGED
    # row; a later edit sees BUILDING and belongs to a following attempt.
    publication.source_revision = scope.source_revision
    publication.source_snapshot = snapshot
    publication.source_fingerprint = fingerprint
    publication.status = DatasetPublication.Status.BUILDING
    publication.save(
        update_fields=("source_revision", "source_snapshot", "source_fingerprint", "status")
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
        publication = DatasetPublication.objects.get(pk=job.build_publication_id)
        if publication.source_snapshot is None:
            # A BUILDING attempt must consume its frozen source.  Retention
            # never clears BUILDING snapshots; treating a missing snapshot as
            # current canonical data here would break that immutable boundary.
            raise PublicationBuildError("Frozen publication source is unavailable.")
        source_fingerprint_value = publication.source_fingerprint
        summary = build_summary(
            definition=definition,
            department=job.department,
            station=job.station,
            source_revision=job.source_revision,
        )
        # This is a cooperative early cancellation checkpoint. The final
        # transaction below remains authoritative for a cancellation that races
        # after this read and before publication activation.
        if not _build_is_still_running(job_id=job.id):
            return PublicationJob.objects.get(pk=job.id)
        is_document_v2 = publication.schema_version == 2 and publication.dataset_type_code in {
            "department_fire_plans", "department_klgv_plans"
        }
        if is_document_v2 and publication.dataset_type_code == "department_klgv_plans":
            # KLGV starts on v2: a legacy ZIP has no lifecycle or protocol purpose.
            artifact = {}
        else:
            artifact = build_encrypted_artifact(
                publication=publication,
                plaintext=build_artifact(
                    definition=definition,
                    department=job.department,
                    station=job.station,
                    source_revision=job.source_revision,
                    source_snapshot=publication.source_snapshot,
                ),
            )
        if is_document_v2:
            from apps.publications.fire_plan_v2 import build_fire_plan_v2_generation
            from apps.publications.fire_plan_v2_delivery import build_fire_plan_v2_manifest

            if publication.dataset_type_code == "department_fire_plans":
                build_fire_plan_v2_generation(publication=publication)
                build_fire_plan_v2_manifest(publication=publication)
            else:
                from apps.publications.document_v2 import (
                    build_document_v2_generation,
                    build_document_v2_manifest,
                )

                build_document_v2_generation(publication=publication)
                build_document_v2_manifest(publication=publication)
            if not _build_is_still_running(job_id=job.id):
                terminal_publication = DatasetPublication.objects.get(pk=publication.id)
                if terminal_publication.status in (
                    DatasetPublication.Status.FAILED,
                    DatasetPublication.Status.CANCELLED,
                    DatasetPublication.Status.REJECTED,
                    DatasetPublication.Status.OBSOLETE,
                ):
                    release_terminal_document_artifact_references(publication=terminal_publication)
        if not _build_is_still_running(job_id=job.id):
            _compensate_artifact(artifact)
            return PublicationJob.objects.get(pk=job.id)
        return finalize_publication_job(
            job_id=job.id,
            summary=summary,
            artifact=artifact,
            source_fingerprint_value=source_fingerprint_value,
        )
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
        remove_artifact_path(artifact.get("artifact_path"))
    except ArtifactError:
        pass


def _build_is_still_running(*, job_id) -> bool:
    """Return whether a worker may continue expensive work for one attempt."""
    return PublicationJob.objects.filter(
        pk=job_id,
        status=PublicationJob.Status.RUNNING,
        build_publication__status=DatasetPublication.Status.BUILDING,
    ).exists()


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
    if not _scope_is_dirty(scope=scope):
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
    *,
    job_id,
    summary: dict[str, object],
    artifact: dict[str, object],
    source_fingerprint_value: str | None = None,
) -> PublicationJob:
    job_preview = PublicationJob.objects.select_related("department", "station").get(pk=job_id)
    scope = _locked_scope(
        department=job_preview.department,
        station=job_preview.station,
        dataset_type_code=job_preview.dataset_type_code,
    )
    job = (
        PublicationJob.objects.select_for_update(of=("self",))
        .select_related("department", "station")
        .get(pk=job_id)
    )
    if job.status != PublicationJob.Status.RUNNING:
        _schedule_artifact_removal(str(artifact.get("artifact_path", "")))
        return job
    if job.build_publication_id is None:
        return fail_publication_job(
            job_id=job.id,
            error_category="worker",
            error_message="Claim did not allocate a publication.",
        )
    now = timezone.now()
    publication = DatasetPublication.objects.select_for_update().get(pk=job.build_publication_id)
    if publication.status != DatasetPublication.Status.BUILDING:
        _schedule_artifact_removal(str(artifact.get("artifact_path", "")))
        return job
    source_fingerprint_value = source_fingerprint_value or publication.source_fingerprint
    definition = get_dataset_definition(job.dataset_type_code)
    validate_built_summary(definition=definition, summary=summary)
    publication.build_summary = summary
    publication.source_fingerprint = source_fingerprint_value
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
    delivery_transition = (
        publication.schema_version == 2
        and previous_current is not None
        and previous_current.schema_version != 2
    )
    if (
        previous_current is not None
        and previous_current.source_fingerprint
        and previous_current.source_fingerprint == source_fingerprint_value
        and not delivery_transition
    ):
        _schedule_artifact_removal(str(artifact.get("artifact_path", "")))
        publication.status = DatasetPublication.Status.OBSOLETE
        publication.source_snapshot = None
        publication.save(update_fields=("status", "source_fingerprint", "source_snapshot"))
        release_terminal_document_artifact_references(publication=publication)
        job.status = PublicationJob.Status.OBSOLETE
        job.completed_at = now
        job.save(update_fields=("status", "completed_at"))
        scope.dirty_since = None
        scope.save(update_fields=("dirty_since", "updated_at"))
        record_event(
            action="publication.job_obsolete",
            department=job.department,
            station=job.station,
            target_type="publication_job",
            target_uuid=job.id,
            metadata={
                "dataset_type_code": job.dataset_type_code,
                "source_revision": job.source_revision,
                "reason": "source_matches_current_publication",
            },
        )
        return job
    if previous_current is not None:
        revoke_publication_dataset_key_grants(publication=previous_current)
        previous_current.status = DatasetPublication.Status.SUPERSEDED
        previous_current.save(update_fields=("status",))
    publication.status = DatasetPublication.Status.PUBLISHED
    publication.published_at = now
    publication.supersedes = previous_current
    publication.full_clean()
    publication.save(
        update_fields=(
            "build_summary",
            "source_fingerprint",
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
    # A canonical edit can commit while this attempt is BUILDING.  Do not clear
    # its existing dirty window merely because the older frozen attempt became
    # current; it is the debounce provenance for the one follow-up candidate.
    source_changed_during_build = _scope_is_dirty(scope=scope)
    scope.dirty_since = None if not source_changed_during_build else scope.dirty_since or now
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
    # A later canonical edit must not rewrite this completed attempt. Once the
    # new active fingerprint is visible, queue one coalescing successor only
    # when the canonical source still differs.
    if source_changed_during_build:
        transaction.on_commit(
            lambda: enqueue_publication_job(
                department=job.department,
                station=job.station,
                dataset_type_code=job.dataset_type_code,
                trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
                debounce_started_at=scope.dirty_since,
            )
        )
    return job


@transaction.atomic
def fail_publication_job(
    *, job_id, error_message: str, error_category: str = "build"
) -> PublicationJob:
    job_preview = PublicationJob.objects.select_related("department", "station").get(pk=job_id)
    _locked_scope(
        department=job_preview.department,
        station=job_preview.station,
        dataset_type_code=job_preview.dataset_type_code,
    )
    job = PublicationJob.objects.select_for_update().get(pk=job_id)
    if job.status != PublicationJob.Status.RUNNING:
        return job
    job.status = PublicationJob.Status.FAILED
    job.completed_at = timezone.now()
    job.error_message = error_message[:2000]
    job.error_category = error_category[:32]
    job.save(update_fields=("status", "completed_at", "error_message", "error_category"))
    if job.build_publication_id is not None:
        # Multi-row worker and administrator lifecycle operations use scope ->
        # job -> publication locking before the attempt row is locked.
        publication = (
            DatasetPublication.objects.select_for_update()
            .filter(pk=job.build_publication_id)
            .first()
        )
        if publication is not None:
            _schedule_artifact_removal(publication.artifact_path)
            if publication.status == DatasetPublication.Status.BUILDING:
                publication.status = DatasetPublication.Status.FAILED
                publication.artifact_status = DatasetPublication.ArtifactStatus.FAILED
                publication.build_error = job.error_message
                publication.save(update_fields=("status", "artifact_status", "build_error"))
                release_terminal_document_artifact_references(publication=publication)
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
    job_ids = list(
        PublicationJob.objects.filter(
            status=PublicationJob.Status.RUNNING,
            heartbeat_at__lt=cutoff,
        ).values_list("id", flat=True)
    )
    recovered = 0
    for job_id in job_ids:
        preview = (
            PublicationJob.objects.select_related("department", "station").filter(pk=job_id).first()
        )
        if preview is None:
            continue
        _locked_scope(
            department=preview.department,
            station=preview.station,
            dataset_type_code=preview.dataset_type_code,
        )
        job = (
            PublicationJob.objects.select_for_update()
            .filter(pk=job_id, status=PublicationJob.Status.RUNNING, heartbeat_at__lt=cutoff)
            .first()
        )
        if job is None:
            continue
        recovered += 1
        if job.build_publication_id is not None:
            publication = (
                DatasetPublication.objects.select_for_update()
                .filter(pk=job.build_publication_id)
                .first()
            )
            if publication is not None and publication.status == DatasetPublication.Status.BUILDING:
                publication.status = DatasetPublication.Status.FAILED
                publication.build_error = "Worker heartbeat timed out."
                publication.save(update_fields=("status", "build_error"))
                release_terminal_document_artifact_references(publication=publication)
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
    return recovered


@transaction.atomic
def publish_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    publication = DatasetPublication.objects.select_related("department", "station").get(
        pk=publication.pk
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
    publication = DatasetPublication.objects.select_for_update().get(pk=publication.pk)
    previous = scope.current_published_publication
    if previous is not None:
        revoke_publication_dataset_key_grants(publication=previous)
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
                allow_clean_rebuild=True,
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
def stage_publication_update(
    *, actor, department, station=None, dataset_type_code: str
) -> DatasetScopeState:
    """Create one new staged attempt only for logically unpublished source content."""
    require_department_admin(actor, department)
    _validate_scope(department=department, station=station, dataset_type_code=dataset_type_code)
    scope = _locked_scope(
        department=department, station=station, dataset_type_code=dataset_type_code
    )
    if not _scope_is_dirty(scope=scope):
        raise PublicationError("This dataset scope has no unpublished changes.")
    live = PublicationJob.objects.select_for_update().filter(
        scope_state=scope,
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    )
    if live.exists():
        raise PublicationError("A publication update is already staged or building.")
    queued = enqueue_publication_job(
        department=department,
        station=station,
        dataset_type_code=dataset_type_code,
        requested_by=actor,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
    )
    if queued is None:
        raise PublicationError("This dataset scope has no unpublished changes.")
    record_event(
        action="publication.update_staged",
        actor_user=actor,
        department=department,
        station=station,
        target_type="dataset_scope_state",
        target_uuid=scope.id,
        metadata={"dataset_type_code": dataset_type_code, "source_revision": scope.source_revision},
    )
    return scope


@transaction.atomic
def build_staged_publication(*, actor, scope: DatasetScopeState) -> PublicationJob:
    """Promote exactly the existing staged attempt; never allocate another version."""
    require_department_admin(actor, scope.department)
    locked_scope = _locked_scope(
        department=scope.department,
        station=scope.station,
        dataset_type_code=scope.dataset_type_code,
    )
    # `build_publication` is nullable.  Preserve scope -> job -> publication
    # locking by loading the job directly and locking the staged attempt in a
    # separate base-row query below.
    job = (
        PublicationJob.objects.select_for_update()
        .filter(scope_state=locked_scope, status=PublicationJob.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if job is None or job.build_publication_id is None:
        raise PublicationError("No staged publication is available to build.")
    publication = (
        DatasetPublication.objects.select_for_update().filter(pk=job.build_publication_id).first()
    )
    if publication is None or publication.status != DatasetPublication.Status.STAGED:
        raise PublicationError("Only a staged publication can be built now.")
    job.trigger_type = PublicationJob.TriggerType.USER_REQUEST
    # `not_before` is the server-authoritative immediate-claim timestamp. The
    # list may poll this staged row only for a bounded grace interval from it.
    job.not_before = timezone.now()
    job.requested_by = actor
    job.save(update_fields=("trigger_type", "not_before", "requested_by"))
    record_event(
        action="publication.build_now_requested",
        actor_user=actor,
        department=locked_scope.department,
        station=locked_scope.station,
        target_type="dataset_publication",
        target_uuid=job.build_publication_id,
        metadata={
            "dataset_type_code": locked_scope.dataset_type_code,
            "version_number": publication.version_number,
        },
    )
    transaction.on_commit(wake_publication_build_worker)
    return job


@transaction.atomic
def delete_staged_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    """Terminalize one unstarted attempt while preserving its immutable identity."""
    candidate = DatasetPublication.objects.select_related("department", "station").get(
        pk=publication.pk
    )
    require_department_admin(actor, candidate.department)
    scope = _locked_scope(
        department=candidate.department,
        station=candidate.station,
        dataset_type_code=candidate.dataset_type_code,
    )
    job = (
        PublicationJob.objects.select_for_update()
        .filter(build_publication=candidate)
        .order_by("-created_at")
        .first()
    )
    candidate = DatasetPublication.objects.select_for_update().get(pk=candidate.pk)
    if candidate.scope_state_id != scope.id or candidate.status != DatasetPublication.Status.STAGED:
        raise PublicationError("Only a staged publication can be deleted.")
    if job is not None and job.status != PublicationJob.Status.PENDING:
        raise PublicationError("A started publication build must be cancelled instead.")
    # An unstarted attempt was never a successful publication.  Retain its
    # immutable identity as CANCELLED; OBSOLETE is reserved for successful
    # publication artifacts retired by lifecycle deletion/retention.
    candidate.status = DatasetPublication.Status.CANCELLED
    candidate.save(update_fields=("status",))
    if job is not None:
        job.status = PublicationJob.Status.CANCELLED
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "completed_at"))
    record_event(
        action="publication.staged_deleted",
        actor_user=actor,
        department=candidate.department,
        station=candidate.station,
        target_type="dataset_publication",
        target_uuid=candidate.id,
        metadata={
            "dataset_type_code": candidate.dataset_type_code,
            "version_number": candidate.version_number,
        },
    )
    return candidate


@transaction.atomic
def cancel_publication_build(*, actor, publication: DatasetPublication) -> DatasetPublication:
    """Authoritatively cancel a running attempt; the worker observes this at closeout."""
    candidate = DatasetPublication.objects.select_related("department", "station").get(
        pk=publication.pk
    )
    require_department_admin(actor, candidate.department)
    scope = _locked_scope(
        department=candidate.department,
        station=candidate.station,
        dataset_type_code=candidate.dataset_type_code,
    )
    job = (
        PublicationJob.objects.select_for_update()
        .filter(build_publication=candidate)
        .order_by("-created_at")
        .first()
    )
    if job is None:
        raise PublicationError("Publication has no build job to cancel.")
    candidate = DatasetPublication.objects.select_for_update().get(pk=candidate.pk)
    if candidate.scope_state_id != scope.id or job.status != PublicationJob.Status.RUNNING:
        raise PublicationError("Only a building publication can be cancelled.")
    if candidate.status != DatasetPublication.Status.BUILDING:
        raise PublicationError("Only a building publication can be cancelled.")
    candidate.status = DatasetPublication.Status.CANCELLED
    candidate.build_error = "Build cancelled by an administrator."
    candidate.save(update_fields=("status", "build_error"))
    release_terminal_document_artifact_references(publication=candidate)
    job.status = PublicationJob.Status.CANCELLED
    job.completed_at = timezone.now()
    job.error_category = "cancelled"
    job.error_message = "Build cancelled by an administrator."
    job.save(update_fields=("status", "completed_at", "error_category", "error_message"))
    _schedule_artifact_removal(candidate.artifact_path)
    record_event(
        action="publication.build_cancelled",
        actor_user=actor,
        department=candidate.department,
        station=candidate.station,
        target_type="dataset_publication",
        target_uuid=candidate.id,
        metadata={
            "dataset_type_code": candidate.dataset_type_code,
            "version_number": candidate.version_number,
        },
    )
    return candidate


def _activate_publication(
    *, scope: DatasetScopeState, target: DatasetPublication, actor, action: str
) -> DatasetPublication:
    previous = scope.current_published_publication
    if previous is not None and previous.pk != target.pk:
        revoke_publication_dataset_key_grants(publication=previous)
        previous.status = DatasetPublication.Status.SUPERSEDED
        previous.save(update_fields=("status",))
    target.status = DatasetPublication.Status.PUBLISHED
    target.published_at = timezone.now()
    target.published_by = actor
    target.save(update_fields=("status", "published_at", "published_by"))
    scope.current_published_publication = target
    scope.save(update_fields=("current_published_publication", "updated_at"))
    PublicationActivation.objects.create(
        publication=target,
        scope_state=scope,
        previous_publication=previous,
        action=action,
        activated_by=actor,
    )
    return target


@transaction.atomic
def rollback_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    publication = DatasetPublication.objects.select_related("department", "station").get(
        pk=publication.pk
    )
    require_department_admin(actor, publication.department)
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
    publication = DatasetPublication.objects.select_for_update().get(pk=publication.pk)
    if (
        publication.scope_state_id != scope.id
        or publication.status != DatasetPublication.Status.SUPERSEDED
    ):
        raise PublicationError("Only a superseded publication can be restored.")
    if publication.artifact_status != DatasetPublication.ArtifactStatus.READY or (
        not publication.artifact_path
        and not (
            publication.schema_version == 2
            and publication.dataset_type_code in {"department_fire_plans", "department_klgv_plans"}
        )
    ):
        raise PublicationError("Rollback target has no usable artifact.")
    previous = scope.current_published_publication
    if previous is None:
        raise PublicationError("Dataset scope has no current publication.")
    if _newer_live_attempt_exists(scope=scope, version_number=previous.version_number):
        raise PublicationError(
            "Rollback is unavailable while a newer attempt is staged or building."
        )
    _activate_publication(
        scope=scope, target=publication, actor=actor, action=PublicationActivation.Action.ROLLBACK
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


@transaction.atomic
def delete_publication(*, actor, publication: DatasetPublication) -> DatasetPublication:
    """Retire an immutable attempt, retaining its row and consuming its version."""
    candidate = DatasetPublication.objects.select_related("department", "station").get(
        pk=publication.pk
    )
    require_department_admin(actor, candidate.department)
    _validate_scope(
        department=candidate.department,
        station=candidate.station,
        dataset_type_code=candidate.dataset_type_code,
    )
    scope = _locked_scope(
        department=candidate.department,
        station=candidate.station,
        dataset_type_code=candidate.dataset_type_code,
    )
    candidate = DatasetPublication.objects.select_for_update().get(pk=candidate.pk)
    if candidate.scope_state_id != scope.id:
        raise PublicationError("Publication is outside this dataset scope.")
    if candidate.status == DatasetPublication.Status.PUBLISHED:
        if scope.current_published_publication_id != candidate.id:
            raise PublicationError("Publication is not the authoritative active publication.")
        if _newer_live_attempt_exists(scope=scope, version_number=candidate.version_number):
            raise PublicationError(
                "Active deletion is unavailable while a newer attempt is staged or building."
            )
        predecessor = _eligible_predecessor(scope=scope, before_version=candidate.version_number)
        if predecessor is None:
            raise PublicationError("Active publication has no safe predecessor to activate.")
        _activate_publication(
            scope=scope,
            target=predecessor,
            actor=actor,
            action=PublicationActivation.Action.ROLLBACK,
        )
        record_event(
            action="publication.active_replaced_before_delete",
            actor_user=actor,
            department=candidate.department,
            station=candidate.station,
            target_type="dataset_publication",
            target_uuid=candidate.id,
            metadata={"replacement_version": predecessor.version_number},
        )
    elif candidate.status != DatasetPublication.Status.SUPERSEDED:
        raise PublicationError("Only active or superseded successful publications can be deleted.")
    artifact_path = candidate.artifact_path
    revoke_publication_dataset_key_grants(publication=candidate)
    candidate.status = DatasetPublication.Status.OBSOLETE
    candidate.source_snapshot = None
    # Artifact metadata is cryptographically immutable once READY. The terminal
    # lifecycle state makes this attempt unavailable to manifests/downloads;
    # leave its signed metadata intact for historical integrity and remove only
    # the ciphertext after the transaction commits.
    candidate.save(update_fields=("status", "source_snapshot"))
    release_terminal_document_artifact_references(publication=candidate)
    if scope.latest_built_publication_id == candidate.id:
        scope.latest_built_publication = scope.current_published_publication
        scope.save(update_fields=("latest_built_publication", "updated_at"))
    _schedule_artifact_removal(artifact_path)
    record_event(
        action="publication.deleted",
        actor_user=actor,
        department=candidate.department,
        station=candidate.station,
        target_type="dataset_publication",
        target_uuid=candidate.id,
        metadata={
            "dataset_type_code": candidate.dataset_type_code,
            "version_number": candidate.version_number,
        },
    )
    return candidate


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
        needs_attention = _scope_is_dirty(scope=scope) or scope.latest_built_publication_id is None
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
