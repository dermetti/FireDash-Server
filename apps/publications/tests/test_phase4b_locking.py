"""PostgreSQL regressions for publication dirtying locks and coalescing."""

import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.personnel.services import create_person, update_person
from apps.publications.builders import build_source_payload, source_fingerprint_for_payload
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition


def _ready_artifact_fields(*, department_id, publication_id):
    return {
        "artifact_ready": True,
        "artifact_status": DatasetPublication.ArtifactStatus.READY,
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


@pytest.fixture
def personnel_publication_context(db):
    admin = User.objects.create_user("phase4-locking@example.test", "Phase 4 Admin", "password")
    department = Department.objects.create(
        name="Phase 4 locking", short_code="P4L", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(
        department=department, name="Locking station", short_code="LCK"
    )
    person = create_person(
        actor=admin,
        department=department,
        home_station=station,
        personnel_number="FB-002",
        first_name="André",
        last_name="Example",
    )
    scope = DatasetScopeState.objects.get(
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )

    # Creating the person initially stages v1.  Model an already-distributed
    # v13 source instead, so the mutations below exercise the real pending
    # candidate/revert path from the production regression.
    initial_job = PublicationJob.objects.get(scope_state=scope)
    initial_publication = initial_job.build_publication
    assert initial_publication is not None
    initial_job.status = PublicationJob.Status.OBSOLETE
    initial_job.completed_at = timezone.now()
    initial_job.save(update_fields=("status", "completed_at"))
    initial_publication.status = DatasetPublication.Status.OBSOLETE
    initial_publication.save(update_fields=("status",))

    definition = get_dataset_definition(scope.dataset_type_code)
    snapshot = build_source_payload(
        definition=definition,
        department=department,
        station=station,
    )
    current_id = uuid.uuid4()
    current = DatasetPublication.objects.create(
        id=current_id,
        department=department,
        station=station,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=13,
        schema_version=definition.current_schema_version,
        source_revision=scope.source_revision,
        source_fingerprint=source_fingerprint_for_payload(snapshot),
        source_snapshot=snapshot,
        status=DatasetPublication.Status.PUBLISHED,
        **_ready_artifact_fields(department_id=department.id, publication_id=current_id),
    )
    scope.current_published_publication = current
    scope.latest_built_publication = current
    scope.current_source_fingerprint = current.source_fingerprint
    scope.save(
        update_fields=(
            "current_published_publication",
            "latest_built_publication",
            "current_source_fingerprint",
        )
    )
    assert scope.current_source_fingerprint == current.source_fingerprint
    return admin, department, station, person, scope, current


@pytest.mark.django_db(transaction=True)
def test_person_edit_then_clean_revert_locks_base_rows_on_postgresql(
    personnel_publication_context,
):
    """A pending nullable build FK must not turn the dirty lock into an outer join."""
    assert connection.vendor == "postgresql"
    admin, _department, _station, person, scope, current = personnel_publication_context

    updated = update_person(
        actor=admin,
        person=person,
        personnel_number="FB-002",
        first_name="Andrée",
        last_name="Example",
    )
    updated.refresh_from_db()
    assert updated.first_name == "Andrée"

    staged = DatasetPublication.objects.get(
        scope_state=scope,
        status=DatasetPublication.Status.STAGED,
    )
    assert staged.version_number == 14
    staged_job = PublicationJob.objects.get(build_publication=staged)
    assert staged_job.status == PublicationJob.Status.PENDING
    scope.refresh_from_db()
    assert scope.current_source_fingerprint == staged.source_fingerprint
    assert scope.current_source_fingerprint != current.source_fingerprint

    reverted = update_person(
        actor=admin,
        person=updated,
        personnel_number="FB-002",
        first_name="André",
        last_name="Example",
    )

    reverted.refresh_from_db()
    scope.refresh_from_db()
    staged.refresh_from_db()
    staged_job.refresh_from_db()
    current.refresh_from_db()
    assert reverted.first_name == "André"
    assert scope.current_published_publication_id == current.id
    assert scope.dirty_since is None
    assert scope.current_source_fingerprint == current.source_fingerprint
    assert staged.status == DatasetPublication.Status.CANCELLED
    assert staged_job.status == PublicationJob.Status.CANCELLED
    assert list(
        DatasetPublication.objects.filter(scope_state=scope)
        .order_by("version_number")
        .values_list("version_number", flat=True)
    ) == [1, 13, 14]
    assert not PublicationJob.objects.filter(
        scope_state=scope,
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_person_edits_coalesce_one_staged_attempt_and_refresh_its_source_snapshot(
    personnel_publication_context,
):
    assert connection.vendor == "postgresql"
    admin, _department, _station, person, scope, _current = personnel_publication_context

    first_edit = update_person(
        actor=admin,
        person=person,
        personnel_number="FB-002",
        first_name="Andrée",
        last_name="Example",
    )
    staged = DatasetPublication.objects.get(
        scope_state=scope,
        status=DatasetPublication.Status.STAGED,
    )
    initial_fingerprint = staged.source_fingerprint
    initial_snapshot = staged.source_snapshot

    update_person(
        actor=admin,
        person=first_edit,
        personnel_number="FB-002",
        first_name="Andréa",
        last_name="Example",
    )

    staged.refresh_from_db()
    job = PublicationJob.objects.get(build_publication=staged)
    assert staged.version_number == 14
    assert staged.status == DatasetPublication.Status.STAGED
    assert staged.source_fingerprint != initial_fingerprint
    assert staged.source_snapshot != initial_snapshot
    scope.refresh_from_db()
    assert scope.current_source_fingerprint == staged.source_fingerprint
    assert job.status == PublicationJob.Status.PENDING
    assert list(
        DatasetPublication.objects.filter(scope_state=scope)
        .order_by("version_number")
        .values_list("version_number", flat=True)
    ) == [1, 13, 14]
    assert (
        PublicationJob.objects.filter(
            scope_state=scope,
            status=PublicationJob.Status.PENDING,
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_person_noop_does_not_recompute_or_change_persisted_scope_fingerprint(
    personnel_publication_context,
):
    admin, _department, _station, person, scope, current = personnel_publication_context
    initial_revision = scope.source_revision
    initial_fingerprint = scope.current_source_fingerprint

    result = update_person(
        actor=admin,
        person=person,
        personnel_number="FB-002",
        first_name=person.first_name,
        last_name=person.last_name,
    )

    result.refresh_from_db()
    scope.refresh_from_db()
    assert result.first_name == person.first_name
    assert scope.source_revision == initial_revision
    assert scope.current_source_fingerprint == initial_fingerprint == current.source_fingerprint
    assert not DatasetPublication.objects.filter(
        scope_state=scope, status=DatasetPublication.Status.STAGED
    ).exists()
