"""Automatic publication debounce / coalescing regression tests."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import (
    claim_next_job,
    fail_publication_job,
    finalize_publication_job,
    mark_dirty,
    request_rebuild,
)


@pytest.fixture
def debounce_context(db):
    admin = User.objects.create_user("debounce@example.test", "Debounce Admin", "safe-password")
    department = Department.objects.create(name="Debounce Dept", short_code="DBC", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station_a = Station.objects.create(department=department, name="Station A", short_code="STA")
    station_b = Station.objects.create(department=department, name="Station B", short_code="STB")
    return admin, department, station_a, station_b


def _hydrant_summary(revision):
    return {"active_count": 0, "source_revision": revision, "status_counts": {}}


def _artifact_metadata(department_id, publication_id):
    return {
        "artifact_path": f"{department_id}/{publication_id}/artifact.bin",
        "artifact_size": 1,
        "artifact_sha256": "a" * 64,
        "artifact_nonce": b"n" * 12,
        "artifact_wrapped_cek": b"w" * 40,
        "artifact_encryption_algorithm": "AES-256-GCM",
        "artifact_wrapping_algorithm": "AES-KW-RFC3394",
        "artifact_kek_version": "test",
        "artifact_signature": b"s" * 64,
        "artifact_signature_algorithm": "Ed25519",
        "artifact_signing_key_version": "test",
    }


@pytest.mark.django_db(transaction=True)
def test_first_change_creates_one_pending_debounced_job(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )

    jobs = PublicationJob.objects.filter(department=department)
    assert jobs.count() == 1
    job = jobs.get()
    assert job.status == PublicationJob.Status.PENDING
    assert job.trigger_type == PublicationJob.TriggerType.DATA_CHANGE
    assert job.not_before is not None and job.not_before > timezone.now()
    assert job.debounce_started_at is not None
    assert job.source_revision == 1


@pytest.mark.django_db(transaction=True)
def test_repeated_edits_coalesce_and_track_latest_revision(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    first_not_before = PublicationJob.objects.get(department=department).not_before

    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )

    jobs = PublicationJob.objects.filter(department=department)
    assert jobs.count() == 1
    job = jobs.get()
    assert job.source_revision == 2
    assert job.not_before is not None and first_not_before is not None
    assert job.not_before == first_not_before


@pytest.mark.django_db(transaction=True)
def test_maximum_deferral_caps_the_debounce_window(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    job = PublicationJob.objects.get(department=department)
    PublicationJob.objects.filter(pk=job.pk).update(
        debounce_started_at=timezone.now() - timedelta(minutes=15)
    )

    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )

    job.refresh_from_db()
    assert job.not_before is not None and job.debounce_started_at is not None
    assert job.not_before.time().isoformat() == "00:05:00"


@pytest.mark.django_db(transaction=True)
def test_debounced_job_is_not_claimable_before_eligibility(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )

    assert claim_next_job() is None


@pytest.mark.django_db(transaction=True)
def test_debounced_job_is_claimable_after_eligibility(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )

    job = claim_next_job()
    assert job is not None
    assert job.status == PublicationJob.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_different_scopes_have_independent_debounce_windows(debounce_context):
    admin, department, station_a, station_b = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    mark_dirty(
        department=department, station=station_b, dataset_type_code="station_personnel", actor=admin
    )

    jobs = list(PublicationJob.objects.filter(department=department))
    assert len(jobs) == 2
    assert {job.station_id for job in jobs} == {station_a.id, station_b.id}
    for job in jobs:
        assert job.status == PublicationJob.Status.PENDING
        assert job.not_before is not None


@pytest.mark.django_db(transaction=True)
def test_user_request_makes_existing_pending_job_immediately_eligible(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )

    request_rebuild(
        actor=admin, department=department, station=station_a, dataset_type_code="station_personnel"
    )

    jobs = PublicationJob.objects.filter(department=department)
    assert jobs.count() == 1
    job = jobs.get()
    assert job.trigger_type == PublicationJob.TriggerType.USER_REQUEST
    assert job.not_before is None


@pytest.mark.django_db(transaction=True)
def test_user_request_without_pending_work_is_immediately_claimable(debounce_context):
    admin, department, station_a, _ = debounce_context
    request_rebuild(
        actor=admin, department=department, station=station_a, dataset_type_code="station_personnel"
    )

    job = claim_next_job()
    assert job is not None
    assert job.trigger_type == PublicationJob.TriggerType.USER_REQUEST


@pytest.mark.django_db(transaction=True)
def test_historical_terminal_candidate_consumes_its_immutable_attempt_version(debounce_context):
    admin, department, station_a, _ = debounce_context
    scope = DatasetScopeState.objects.create(
        department=department, station=station_a, dataset_type_code="station_personnel"
    )
    # Historical terminal attempts retain their signed version and it remains
    # part of the monotonically increasing attempt sequence.
    DatasetPublication.objects.create(
        department=department,
        station=station_a,
        dataset_type_code="station_personnel",
        scope_state=scope,
        version_number=99,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.REJECTED,
    )
    request_rebuild(
        actor=admin, department=department, station=station_a, dataset_type_code="station_personnel"
    )

    job = claim_next_job()

    assert job is not None
    assert job.build_publication is not None
    assert job.build_publication.version_number == 100


@pytest.mark.django_db(transaction=True)
def test_edits_during_running_build_requeue_after_obsolete(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    # A further edit while RUNNING must not create a second active job.
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    assert PublicationJob.objects.filter(status__in=("PENDING", "RUNNING")).count() == 1

    stale = finalize_publication_job(
        job_id=job.id,
        summary=_hydrant_summary(job.source_revision),
        artifact=_artifact_metadata(department.id, job.build_publication_id),
    )
    stale.refresh_from_db()
    assert stale.status == PublicationJob.Status.OBSOLETE
    pending = PublicationJob.objects.filter(
        department=department, status=PublicationJob.Status.PENDING
    )
    assert pending.count() == 1
    assert pending.get().source_revision == 2


@pytest.mark.django_db(transaction=True)
def test_rolled_back_mutation_creates_no_job(debounce_context):
    admin, department, station_a, _ = debounce_context
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            mark_dirty(
                department=department,
                station=station_a,
                dataset_type_code="station_personnel",
                actor=admin,
            )
            raise RuntimeError("simulate rollback")

    assert PublicationJob.objects.filter(department=department).count() == 0
    assert DatasetScopeState.objects.filter(department=department).count() == 0


@pytest.mark.django_db(transaction=True)
def test_running_build_failure_queues_latest_follow_up(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    # A source change while the build is RUNNING cannot create a second active job.
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    assert PublicationJob.objects.filter(status__in=("PENDING", "RUNNING")).count() == 1

    fail_publication_job(job_id=job.id, error_message="boom")

    job.refresh_from_db()
    assert job.status == PublicationJob.Status.FAILED

    follow_ups = PublicationJob.objects.filter(
        department=department, status=PublicationJob.Status.PENDING
    )
    assert follow_ups.count() == 1
    follow_up = follow_ups.get()
    assert follow_up.source_revision == 2
    assert follow_up.trigger_type == PublicationJob.TriggerType.DATA_CHANGE
    assert follow_up.not_before is not None and follow_up.not_before > timezone.now()
    assert follow_up.debounce_started_at is not None

    scope = DatasetScopeState.objects.get(
        department=department, station=station_a, dataset_type_code="station_personnel"
    )
    assert scope.dirty_since is not None


@pytest.mark.django_db(transaction=True)
def test_multiple_changes_during_running_failure_coalesce_into_one_follow_up(debounce_context):
    admin, department, station_a, _ = debounce_context
    mark_dirty(
        department=department, station=station_a, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    for _ in range(3):
        mark_dirty(
            department=department,
            station=station_a,
            dataset_type_code="station_personnel",
            actor=admin,
        )

    fail_publication_job(job_id=job.id, error_message="boom")

    job.refresh_from_db()
    assert job.status == PublicationJob.Status.FAILED
    follow_ups = PublicationJob.objects.filter(
        department=department, status=PublicationJob.Status.PENDING
    )
    assert follow_ups.count() == 1
    assert follow_ups.get().source_revision == 4
