import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import cast

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.forms import ChoiceField
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import PersonnelStationAssignment
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.publications.builders import build_summary
from apps.publications.feature_services import set_department_feature
from apps.publications.forms import RebuildRequestForm
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    DatasetTypeRegistry,
    PublicationActivation,
    PublicationJob,
)
from apps.publications.registry import (
    DATASET_REGISTRY,
    DatasetRegistryError,
    get_dataset_definition,
    validate_dataset_scope,
)
from apps.publications.services import (
    PublicationError,
    claim_next_job,
    enqueue_publication_job,
    finalize_publication_job,
    mark_dirty,
    publish_publication,
    recover_stale_jobs,
    reject_publication,
    request_rebuild,
    rollback_publication,
)


@pytest.fixture
def publication_context(db):
    admin = User.objects.create_user(
        "publication-admin@example.test", "Publication Admin", "safe-password"
    )
    outsider = User.objects.create_user(
        "publication-outsider@example.test", "Publication Outsider", "safe-password"
    )
    department = Department.objects.create(
        name="Publication Department", short_code="PUB", created_by=admin
    )
    other_department = Department.objects.create(
        name="Other Department", short_code="OTH", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(
        department=department, name="Publication Station", short_code="PS"
    )
    other_station = Station.objects.create(
        department=other_department, name="Other Station", short_code="OS"
    )
    return admin, outsider, department, station, other_station


def ready_publication(*, department, scope, version_number, actor):
    publication_id = uuid.uuid4()
    return DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=scope,
        version_number=version_number,
        schema_version=1,
        source_revision=scope.source_revision,
        status=DatasetPublication.Status.READY_FOR_REVIEW,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        **artifact_metadata(department_id=department.id, publication_id=publication_id),
        created_by=actor,
    )


def artifact_metadata(*, department_id, publication_id):
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


def hydrant_summary(source_revision):
    return {
        "active_count": 0,
        "source_revision": source_revision,
        "status_counts": {},
    }


@pytest.mark.django_db(transaction=True)
def test_registry_and_model_validation_enforce_registered_scopes(publication_context):
    _, _, department, station, other_station = publication_context

    assert get_dataset_definition("department_hydrants").scope == "department"
    definition = validate_dataset_scope(dataset_type_code="station_personnel", station=station)
    assert definition.code == "station_personnel"
    with pytest.raises(DatasetRegistryError, match="Unknown dataset"):
        get_dataset_definition("unknown")
    with pytest.raises(DatasetRegistryError, match="cannot have a station"):
        validate_dataset_scope(dataset_type_code="department_hydrants", station=station)
    with pytest.raises(DatasetRegistryError, match="require a station"):
        validate_dataset_scope(dataset_type_code="station_personnel", station=None)

    scope = DatasetScopeState(
        department=department, station=station, dataset_type_code="department_hydrants"
    )
    with pytest.raises(ValidationError, match="cannot have a station"):
        scope.full_clean()

    invalid_publication = DatasetPublication(
        department=department,
        station=other_station,
        dataset_type_code="station_personnel",
        version_number=1,
        schema_version=99,
        source_revision=0,
    )
    with pytest.raises(ValidationError) as error:
        invalid_publication.full_clean(exclude={"build_summary", "change_summary", "scope_state"})
    assert error.value.message_dict == {
        "station": ["Station must belong to the publication department."],
        "schema_version": ["Schema version is not supported for this dataset."],
    }


@pytest.mark.django_db(transaction=True)
def test_mark_dirty_advances_revision_preserves_first_dirty_time_and_coalesces_jobs(
    publication_context,
):
    admin, _, department, _, _ = publication_context

    first = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    first_dirty_since = first.dirty_since
    second = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)

    assert second.id == first.id
    assert second.source_revision == 2
    assert second.dirty_since == first_dirty_since
    assert PublicationJob.objects.filter(status=PublicationJob.Status.PENDING).count() == 1
    coalesced = enqueue_publication_job(
        department=department, dataset_type_code="department_hydrants", requested_by=admin
    )
    assert coalesced is not None
    assert coalesced.source_revision == 2


@pytest.mark.django_db(transaction=True)
def test_revision_only_change_during_build_does_not_obsolete_identical_source(publication_context):
    admin, _, department, _, _ = publication_context
    scope = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(minutes=1)
    )
    job = claim_next_job()

    assert job is not None
    assert job.build_publication is not None
    assert job.status == PublicationJob.Status.RUNNING
    assert job.attempt_count == 1
    assert job.build_publication.version_number == 1

    # A monotonic revision alone is not canonical publication content.  The
    # source snapshot is still identical, so the frozen attempt remains valid.
    mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    finalized = finalize_publication_job(
        job_id=job.id,
        summary=hydrant_summary(job.source_revision),
        artifact=artifact_metadata(
            department_id=department.id, publication_id=job.build_publication_id
        ),
    )
    finalized.refresh_from_db()
    assert finalized.build_publication is not None
    finalized.build_publication.refresh_from_db()
    scope.refresh_from_db()

    assert finalized.status == PublicationJob.Status.SUCCEEDED
    assert finalized.build_publication.status == DatasetPublication.Status.PUBLISHED
    assert scope.source_revision == 2
    assert not PublicationJob.objects.filter(status=PublicationJob.Status.PENDING).exists()


@pytest.mark.django_db(transaction=True)
def test_finalize_current_claim_marks_publication_ready_and_clears_dirty_scope(publication_context):
    admin, _, department, _, _ = publication_context
    scope = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(minutes=1)
    )
    job = claim_next_job()

    assert job is not None
    assert job.build_publication is not None
    finalized = finalize_publication_job(
        job_id=job.id,
        summary=hydrant_summary(job.source_revision),
        artifact=artifact_metadata(
            department_id=department.id, publication_id=job.build_publication_id
        ),
    )
    finalized.refresh_from_db()
    assert finalized.build_publication is not None
    finalized.build_publication.refresh_from_db()
    scope.refresh_from_db()

    assert finalized.status == PublicationJob.Status.SUCCEEDED
    assert finalized.build_publication.status == DatasetPublication.Status.PUBLISHED
    assert finalized.build_publication.build_summary == hydrant_summary(scope.source_revision)
    assert scope.latest_built_publication == finalized.build_publication
    assert scope.dirty_since is None


@pytest.mark.django_db(transaction=True)
def test_recover_stale_job_requeues_then_exhausts_attempt_limit(publication_context):
    admin, _, department, _, _ = publication_context
    mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    PublicationJob.objects.filter(department=department).update(
        not_before=timezone.now() - timedelta(minutes=1)
    )
    job = claim_next_job()
    assert job is not None
    assert job.build_publication_id is not None
    old_heartbeat = timezone.now() - timedelta(minutes=2)
    failed_publication_id = job.build_publication_id
    PublicationJob.objects.filter(pk=job.pk).update(heartbeat_at=old_heartbeat)

    assert recover_stale_jobs(timeout=timedelta(minutes=1), max_attempts=2) == 1
    job.refresh_from_db()
    assert job.status == PublicationJob.Status.PENDING
    assert job.build_publication is None
    failed_publication = DatasetPublication.objects.get(pk=failed_publication_id)
    assert failed_publication.status == DatasetPublication.Status.FAILED

    claimed_again = claim_next_job()
    assert claimed_again is not None
    PublicationJob.objects.filter(pk=claimed_again.pk).update(heartbeat_at=old_heartbeat)
    assert recover_stale_jobs(timeout=timedelta(minutes=1), max_attempts=2) == 1
    claimed_again.refresh_from_db()
    assert claimed_again.status == PublicationJob.Status.FAILED
    assert claimed_again.error_category == "retry_exhausted"


@pytest.mark.django_db(transaction=True)
def test_ready_artifact_publications_can_be_published_after_phase7(publication_context):
    admin, _, department, _, _ = publication_context
    scope = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    publication = ready_publication(
        department=department, scope=scope, version_number=1, actor=admin
    )
    published = publish_publication(actor=admin, publication=publication)
    rejected = reject_publication(
        actor=admin,
        publication=ready_publication(
            department=department, scope=scope, version_number=2, actor=admin
        ),
    )
    scope.refresh_from_db()

    assert published.status == DatasetPublication.Status.PUBLISHED
    assert scope.current_published_publication == published
    assert rejected.status == DatasetPublication.Status.REJECTED
    assert PublicationActivation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_publication_actions_require_department_admin(publication_context):
    admin, outsider, department, _, _ = publication_context
    scope = mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)
    publication = ready_publication(
        department=department, scope=scope, version_number=1, actor=admin
    )

    with pytest.raises(PermissionDenied, match="Department administrator scope"):
        publish_publication(actor=outsider, publication=publication)
    with pytest.raises(PermissionDenied, match="Department administrator scope"):
        request_rebuild(
            actor=outsider, department=department, dataset_type_code="department_hydrants"
        )
    with pytest.raises(PublicationError, match="Only a superseded"):
        rollback_publication(actor=admin, publication=publication)


@pytest.mark.django_db(transaction=True)
def test_builders_return_only_registered_summary_fields_without_artifacts(publication_context):
    admin, _, department, station, _ = publication_context
    person = Person.objects.create(department=department)
    PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.TEMPORARY,
        valid_from=timezone.now(),
        valid_until=timezone.now() + timedelta(hours=1),
        created_by=admin,
    )

    for code, scoped_station in (
        ("department_hydrants", None),
        ("department_fire_plans", None),
        ("station_personnel", station),
        ("test_department_incidents", None),
    ):
        definition = get_dataset_definition(code)
        summary = build_summary(
            definition=definition,
            department=department,
            station=scoped_station,
            source_revision=7,
        )
        assert set(summary) == set(definition.summary_schema)
        assert "artifact" not in " ".join(summary).lower()
        assert all(not isinstance(value, bytes | bytearray) for value in summary.values())


def test_internal_dataset_is_not_a_production_rebuild_choice():
    form = RebuildRequestForm(department=uuid.uuid4())
    field = cast(ChoiceField, form.fields["dataset_type_code"])
    choices = {value for value, _label in cast(Iterable[tuple[str, str]], field.choices)}

    assert "test_department_incidents" not in choices


@pytest.mark.django_db(transaction=True)
def test_flush_restores_dataset_type_registry_projection():
    call_command("flush", interactive=False)

    assert set(DatasetTypeRegistry.objects.values_list("code", flat=True)) == set(DATASET_REGISTRY)


@pytest.mark.django_db(transaction=True)
def test_expire_temporary_assignments_command_ends_expired_assignments(publication_context, capsys):
    admin, _, department, station, _ = publication_context
    person = Person.objects.create(department=department)
    expired = PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.TEMPORARY,
        valid_from=timezone.now() - timedelta(hours=2),
        valid_until=timezone.now() - timedelta(hours=1),
        created_by=admin,
    )
    current = PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type=PersonnelStationAssignment.AssignmentType.TEMPORARY,
        valid_from=timezone.now(),
        valid_until=timezone.now() + timedelta(hours=1),
        created_by=admin,
    )

    call_command("expire_temporary_assignments")
    expired.refresh_from_db()
    current.refresh_from_db()

    assert expired.ended_at is not None
    assert expired.ended_by is None
    assert current.ended_at is None
    assert "Expired 1 temporary assignment(s)." in capsys.readouterr().out


@pytest.mark.django_db(transaction=True)
def test_registry_projection_trigger_enforces_scope_and_station_ownership(publication_context):
    admin, _, department, station, other_station = publication_context

    # The fourth, test-only registry record is department scoped without changing SQL checks.
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="test_department_incidents"
    )
    assert scope.dataset_type_code == "test_department_incidents"
    with pytest.raises(DatabaseError, match="Dataset type station scope is invalid"):
        with transaction.atomic():
            DatasetScopeState.objects.create(
                department=department,
                station=station,
                dataset_type_code="test_department_incidents",
            )
    with pytest.raises(DatabaseError, match="Station must belong to the scope department"):
        with transaction.atomic():
            DatasetScopeState.objects.create(
                department=department, station=other_station, dataset_type_code="station_personnel"
            )
    with pytest.raises(DatabaseError, match="Unknown dataset type code"):
        with transaction.atomic():
            DatasetScopeState.objects.create(
                department=department, dataset_type_code="not_registered"
            )


@pytest.mark.django_db(transaction=True)
def test_department_feature_gate_is_audited(publication_context):
    admin, _, department, _, _ = publication_context
    feature = set_department_feature(
        actor=admin, department=department, feature_code="publications", enabled=False
    )
    assert not feature.enabled
    assert AuditEvent.objects.filter(
        action="publication.feature_updated", department=department
    ).exists()
    with pytest.raises(PublicationError, match="has not enabled"):
        mark_dirty(department=department, dataset_type_code="department_hydrants", actor=admin)


@pytest.mark.django_db(transaction=True)
def test_publication_clean_rejects_flat_artifact_path(publication_context):
    admin, _, department, _, _ = publication_context
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    publication = DatasetPublication(
        id=uuid.uuid4(),
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=scope,
        version_number=1,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.READY_FOR_REVIEW,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        **artifact_metadata(department_id=department.id, publication_id=uuid.uuid4()),
        created_by=admin,
    )
    publication.artifact_path = f"{uuid.uuid4()}.bin"

    with pytest.raises(ValidationError) as error:
        publication.full_clean()

    assert "artifact_path" in error.value.message_dict
