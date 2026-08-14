"""Bulk "Rebuild affected datasets" eligibility and deduplication tests."""

import uuid

import pytest

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.publications.services import bulk_request_rebuilds, mark_dirty


@pytest.fixture
def bulk_context(db):
    admin = User.objects.create_user("bulk@example.test", "Bulk Admin", "safe-password")
    department = Department.objects.create(name="Bulk Dept", short_code="BLK", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    station_b = Station.objects.create(department=department, name="Station B", short_code="STB")
    return admin, department, station, station_b


def _published(department, scope, station=None, version_number=1):
    return DatasetPublication.objects.create(
        id=uuid.uuid4(),
        department=department,
        station=station,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path=f"{department.id}/{uuid.uuid4()}/artifact.bin",
        artifact_size=1,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"w" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="1",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="1",
    )


@pytest.mark.django_db(transaction=True)
def test_bulk_rebuild_requests_only_affected_scopes(bulk_context):
    admin, department, station, station_b = bulk_context

    DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )

    failed_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    DatasetPublication.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=failed_scope,
        version_number=1,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.FAILED,
    )

    current_scope = DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    current = _published(department, current_scope, station=station)
    current_scope.latest_built_publication = current
    current_scope.current_published_publication = current
    current_scope.save(update_fields=("latest_built_publication", "current_published_publication"))

    # An active pending job must be left untouched and counted as already queued.
    mark_dirty(
        department=department,
        station=station_b,
        dataset_type_code="station_personnel",
        actor=admin,
    )

    result = bulk_request_rebuilds(actor=admin, department=department)

    assert result == {"requested": 2, "already_queued": 1, "already_current": 1}


@pytest.mark.django_db(transaction=True)
def test_bulk_rebuild_does_not_request_current_scopes(bulk_context):
    admin, department, station, _ = bulk_context
    current_scope = DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    current = _published(department, current_scope, station=station)
    current_scope.latest_built_publication = current
    current_scope.current_published_publication = current
    current_scope.save(update_fields=("latest_built_publication", "current_published_publication"))

    result = bulk_request_rebuilds(actor=admin, department=department)

    assert result == {"requested": 0, "already_queued": 0, "already_current": 1}
