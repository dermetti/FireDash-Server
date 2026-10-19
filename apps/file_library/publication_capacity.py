"""One authoritative capacity policy for file-library publication candidates.

Call under capacity -> scope -> library -> review -> installation locks. No PDF
processing, encryption or filesystem work belongs inside this boundary.
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.authorization.services import require_department_admin
from apps.file_library import capacity
from apps.file_library.models import CapacityReview, Library

FILE_DATASETS = {"department_files", "station_files"}


def lock_scope_capacity(*, department, dataset_type_code):
    if dataset_type_code in FILE_DATASETS:
        capacity.lock_department_capacity(department.id, reassess=False)


def candidate_operation(scope, snapshot):
    return {
        "kind": "publication",
        "name": snapshot["library"]["name"],
        "dataset_type_code": scope.dataset_type_code,
        "source_revision": snapshot["source_revision"],
        "content_fingerprint": capacity.fingerprint(snapshot),
    }


def assess(*, scope, snapshot, actor=None, publication=None, consume=False):
    """Return (admitted, review). Re-evaluate immediately before promotion too.

    A review records the frozen source and complete risk/identity fingerprint.
    Repeated unchanged assessments reuse the row and emit no duplicate audit.
    Historical upload reviews are excluded by the explicit snapshot marker.
    """
    if scope.dataset_type_code not in FILE_DATASETS:
        return True, None
    from apps.file_library.services import active_temporary_access

    library = Library.objects.select_for_update().get(pk=snapshot["library"]["id"])
    if (
        library.department_id != (scope.department_id if scope.station_id is None else None)
        or library.station_id != scope.station_id
    ):
        raise PermissionDenied("Publication library does not belong to this scope.")
    reviews = list(
        CapacityReview.objects.select_for_update()
        .filter(
            library=library,
            publication_snapshot__isnull=False,
            superseded_at__isnull=True,
            consumed_at__isnull=True,
        )
        .order_by("created_at", "id")
    )
    if library.source_revision != snapshot["source_revision"]:
        # A finishing older build must not supersede the newer pending review.
        for review in reviews:
            if review.publication_snapshot == snapshot:
                review.superseded_at = timezone.now()
                review.save(update_fields=("superseded_at",))
                capacity.event(review, actor, "superseded")
        return False, None
    operation = candidate_operation(scope, snapshot)
    projection = capacity.project(library, operation, candidate_snapshot=snapshot)
    digest = capacity.fingerprint(projection)
    current = None
    for review in reviews:
        if (
            review.fingerprint == digest
            and review.publication_snapshot == snapshot
            and (
                publication is None
                or review.publication_id is None
                or review.publication_id == publication.id
            )
        ):
            current = review
        else:
            review.superseded_at = timezone.now()
            review.save(update_fields=("superseded_at",))
            capacity.event(review, actor, "superseded")
    risky = any(tablet["reason"] for tablet in projection["tablets"])
    if not risky:
        if current:
            current.superseded_at = timezone.now()
            current.save(update_fields=("superseded_at",))
            capacity.event(current, actor, "cleared")
        return True, None
    if current is None:
        current = CapacityReview.objects.create(
            library=library,
            requested_by=actor,
            operation=operation,
            projection=projection,
            fingerprint=digest,
            publication_snapshot=snapshot,
            expires_at=None,
        )
        capacity.event(current, actor, "requested")
    admitted = current.decision == "approved"
    if admitted:
        try:
            require_department_admin(current.decided_by, capacity.department_for(library))
            if active_temporary_access(current.decided_by):
                admitted = False
        except PermissionDenied:
            admitted = False
        if current.publication_id and (
            publication is None or current.publication_id != publication.id
        ):
            # Scheduling may inspect a running candidate; it cannot transfer its grant.
            admitted = False
        if not admitted and (publication is not None or current.publication_id is None):
            current.superseded_at = timezone.now()
            current.save(update_fields=("superseded_at",))
            capacity.event(current, actor, "superseded")
            return assess(scope=scope, snapshot=snapshot, actor=actor, publication=publication)
        if admitted and publication is not None:
            current.publication = publication
            if consume:
                current.consumed_at = timezone.now()
            current.save(update_fields=("publication", "consumed_at"))
            if consume:
                capacity.event(current, actor, "consumed")
    return admitted, current


@transaction.atomic
def reassess_department(department_id):
    """Refresh queued candidates after committed eligibility/telemetry/source changes.

    No builds run in the mutation/check-in request. The existing worker owns
    admission and encryption. Claim-time rechecks also cover clock-based expiry.
    """
    from apps.file_library.publication import source_payload
    from apps.publications.models import DatasetScopeState, PublicationJob
    from apps.publications.wake import wake_publication_build_worker

    capacity.lock_department_capacity(department_id, reassess=False)
    for scope in (
        DatasetScopeState.objects.filter(
            department_id=department_id,
            dataset_type_code__in=FILE_DATASETS,
            publication_jobs__status=PublicationJob.Status.PENDING,
        )
        .distinct()
        .order_by("id")
    ):
        scope = DatasetScopeState.objects.select_for_update().get(pk=scope.pk)
        snapshot = source_payload(department=scope.department, station=scope.station)
        had_review = CapacityReview.objects.filter(
            library_id=snapshot["library"]["id"],
            publication_snapshot__isnull=False,
            superseded_at__isnull=True,
            consumed_at__isnull=True,
        ).exists()
        admitted, _review = assess(scope=scope, snapshot=snapshot)
        if admitted and had_review:
            PublicationJob.objects.filter(scope_state=scope, status="PENDING").update(
                not_before=timezone.now()
            )
            transaction.on_commit(wake_publication_build_worker)


def schedule_reassessment(department_id):
    # A failed refresh cannot undo a committed source write. Admission always
    # rechecks under the same lock, so the worker also recovers missed refreshes.
    transaction.on_commit(lambda: reassess_department(department_id), robust=True)


def retire_attempt_review(publication, *, actor=None):
    """A failed/cancelled attempt cannot retain actionable authorization.

    Called by lifecycle services under the capacity and scope locks. Match the
    exact snapshot so a finishing older attempt cannot retire a newer review.
    """
    if publication.dataset_type_code not in FILE_DATASETS or not publication.source_snapshot:
        return
    library = Library.objects.select_for_update().get(
        pk=publication.source_snapshot["library"]["id"]
    )
    for review in CapacityReview.objects.select_for_update().filter(
        library=library,
        publication_snapshot=publication.source_snapshot,
        superseded_at=None,
        consumed_at=None,
    ):
        review.superseded_at = timezone.now()
        review.save(update_fields=("superseded_at",))
        capacity.event(review, actor, "superseded")
