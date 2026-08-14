"""Worker failure-consistency and artifact-compensation tests."""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications import services
from apps.publications.models import DatasetPublication, PublicationJob
from apps.publications.services import build_claimed_job, claim_next_job, mark_dirty


class _GuardError(Exception):
    """Mimic a PostgreSQL trigger ``RAISE EXCEPTION`` (SQLSTATE P0001)."""

    sqlstate = "P0001"


def _guard_integrity_error(message: str) -> IntegrityError:
    error = IntegrityError(message)
    error.__cause__ = _GuardError(message)
    return error


@pytest.fixture
def worker_context(db):
    admin = User.objects.create_user("worker@example.test", "Worker Admin", "safe-password")
    department = Department.objects.create(name="Worker Dept", short_code="WKD", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    return admin, department, station


@pytest.mark.django_db(transaction=True)
def test_finalization_integrity_error_fails_job_and_compensates_artifact(
    worker_context, monkeypatch
):
    admin, department, station = worker_context
    mark_dirty(
        department=department, station=station, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    artifact_path = f"{department.id}/{job.build_publication_id}/artifact.bin"
    removed = []

    monkeypatch.setattr(
        services,
        "build_encrypted_artifact",
        lambda **kwargs: {"artifact_path": artifact_path},
    )
    monkeypatch.setattr(
        services,
        "finalize_publication_job",
        lambda **kwargs: (_ for _ in ()).throw(
            _guard_integrity_error("Artifact path must be a generated publication path")
        ),
    )
    monkeypatch.setattr(services, "remove_artifact_path", lambda path: removed.append(path))

    result = build_claimed_job(job_id=job.id)

    job.refresh_from_db()
    assert job.build_publication_id is not None
    publication = DatasetPublication.objects.get(pk=job.build_publication_id)
    assert result.status == PublicationJob.Status.FAILED
    assert job.status == PublicationJob.Status.FAILED
    assert publication.status == DatasetPublication.Status.FAILED
    assert removed == [artifact_path]


@pytest.mark.django_db(transaction=True)
def test_systemic_integrity_error_propagates(worker_context, monkeypatch):
    admin, department, station = worker_context
    mark_dirty(
        department=department, station=station, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    monkeypatch.setattr(
        services,
        "build_encrypted_artifact",
        lambda **kwargs: {"artifact_path": "x/y/artifact.bin"},
    )
    monkeypatch.setattr(
        services,
        "finalize_publication_job",
        lambda **kwargs: (_ for _ in ()).throw(
            IntegrityError("duplicate key value violates unique constraint")
        ),
    )

    with pytest.raises(IntegrityError):
        build_claimed_job(job_id=job.id)

    # A non-guard IntegrityError must not be silently converted into a FAILED job.
    job.refresh_from_db()
    assert job.status == PublicationJob.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_build_failure_does_not_leave_stuck_running_or_building(worker_context, monkeypatch):
    admin, department, station = worker_context
    mark_dirty(
        department=department, station=station, dataset_type_code="station_personnel", actor=admin
    )
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    job = claim_next_job()
    assert job is not None

    monkeypatch.setattr(
        services,
        "build_encrypted_artifact",
        lambda **kwargs: (_ for _ in ()).throw(
            services.ArtifactError("Could not promote publication artifact.")
        ),
    )

    result = build_claimed_job(job_id=job.id)

    job.refresh_from_db()
    assert job.build_publication_id is not None
    publication = DatasetPublication.objects.get(pk=job.build_publication_id)
    assert result.status == PublicationJob.Status.FAILED
    assert job.status == PublicationJob.Status.FAILED
    assert publication.status == DatasetPublication.Status.FAILED
    assert publication.artifact_status == DatasetPublication.ArtifactStatus.FAILED
