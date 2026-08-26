"""Immutable, scope-wide publication-attempt versioning regression tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications import artifacts
from apps.publications.artifacts import _signature_payload, build_encrypted_artifact
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import (
    claim_next_job,
    finalize_publication_job,
    mark_dirty,
    request_rebuild,
)


def _summary(revision: int) -> dict[str, object]:
    return {"active_count": 0, "source_revision": revision, "status_counts": {}}


def _verify_signature(*, publication: DatasetPublication, root: Path, signing_seed: bytes) -> None:
    ciphertext = (root / publication.artifact_path).read_bytes()
    Ed25519PrivateKey.from_private_bytes(signing_seed).public_key().verify(
        bytes(publication.artifact_signature or b""),
        _signature_payload(
            publication=publication,
            wrapped_cek=bytes(publication.artifact_wrapped_cek or b""),
            nonce=bytes(publication.artifact_nonce or b""),
            ciphertext=ciphertext,
        ),
    )


def _signed_publication(
    *,
    department: Department,
    scope: DatasetScopeState,
    version: int,
    status: str,
    dataset_type_code: str = "department_hydrants",
    validate: bool = True,
) -> DatasetPublication:
    publication = DatasetPublication.objects.create(
        department=department,
        station=scope.station,
        dataset_type_code=dataset_type_code,
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=scope.source_revision,
        source_snapshot={"test_attempt_version": version},
        status=DatasetPublication.Status.BUILDING,
        build_summary=_summary(scope.source_revision),
        change_summary={"changed": 0},
    )
    metadata = build_encrypted_artifact(publication=publication, plaintext=f"v{version}".encode())
    for field, value in metadata.items():
        setattr(publication, field, value)
    publication.artifact_ready = True
    publication.artifact_status = DatasetPublication.ArtifactStatus.READY
    publication.status = status
    if status == DatasetPublication.Status.PUBLISHED:
        publication.published_at = timezone.now()
    if validate:
        publication.full_clean()
    publication.save()
    return publication


def _settings(tmp_path):
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    return override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "publications",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "publications" / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="test",
        PUBLICATION_SIGNING_KEY_VERSION="test",
    )


def _context(*, station_scoped: bool = False):
    admin = User.objects.create_user("attempts@example.test", "Attempt Admin", "safe-password")
    department = Department.objects.create(name="Attempts", short_code="ATT", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = (
        Station.objects.create(department=department, name="Station", short_code="STN")
        if station_scoped
        else None
    )
    dataset_type_code = "station_personnel" if station_scoped else "department_hydrants"
    scope = DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code=dataset_type_code
    )
    return admin, department, scope, dataset_type_code


@pytest.mark.django_db(transaction=True)
def test_failed_v13_is_retained_and_next_successful_attempt_is_v14_with_valid_signature(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda _path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda _path: None)
    admin, department, scope, dataset_type_code = _context()
    signing_seed = b"s" * 32
    root = tmp_path / "publications"

    with _settings(tmp_path):
        current = _signed_publication(
            department=department,
            scope=scope,
            version=12,
            status=DatasetPublication.Status.PUBLISHED,
        )
        scope.current_published_publication = current
        scope.latest_built_publication = current
        scope.save(update_fields=("current_published_publication", "latest_built_publication"))
        failed = _signed_publication(
            department=department,
            scope=scope,
            version=13,
            status=DatasetPublication.Status.FAILED,
        )
        failed_signature = failed.artifact_signature
        failed_ciphertext = (root / failed.artifact_path).read_bytes()
        _verify_signature(publication=failed, root=root, signing_seed=signing_seed)

        with patch("apps.publications.services.wake_publication_build_worker"):
            request_rebuild(actor=admin, department=department, dataset_type_code=dataset_type_code)
        job = claim_next_job()
        assert job is not None and job.build_publication is not None
        assert job.build_publication.version_number == 14
        metadata = build_encrypted_artifact(publication=job.build_publication, plaintext=b"v14")
        finalize_publication_job(
            job_id=job.id, summary=_summary(job.source_revision), artifact=metadata
        )

        failed.refresh_from_db()
        job.build_publication.refresh_from_db()
        assert failed.status == DatasetPublication.Status.FAILED
        assert failed.version_number == 13
        assert failed.artifact_signature == failed_signature
        assert (root / failed.artifact_path).read_bytes() == failed_ciphertext
        _verify_signature(publication=failed, root=root, signing_seed=signing_seed)
        assert job.build_publication.status == DatasetPublication.Status.PUBLISHED
        assert job.build_publication.version_number == 14
        _verify_signature(publication=job.build_publication, root=root, signing_seed=signing_seed)


@pytest.mark.django_db(transaction=True)
def test_failed_and_obsolete_versions_remain_assigned_and_next_attempt_skips_them():
    admin, department, scope, dataset_type_code = _context()
    DatasetPublication.objects.create(
        department=department,
        dataset_type_code=dataset_type_code,
        scope_state=scope,
        version_number=13,
        schema_version=1,
        source_revision=0,
        status=DatasetPublication.Status.FAILED,
    )
    obsolete = DatasetPublication.objects.create(
        department=department,
        dataset_type_code=dataset_type_code,
        scope_state=scope,
        version_number=14,
        schema_version=1,
        source_revision=0,
        status=DatasetPublication.Status.OBSOLETE,
    )

    with patch("apps.publications.services.wake_publication_build_worker"):
        request_rebuild(actor=admin, department=department, dataset_type_code=dataset_type_code)
    job = claim_next_job()

    assert job is not None and job.build_publication is not None
    assert job.build_publication.version_number == 15
    obsolete.refresh_from_db()
    assert obsolete.version_number == 14


@pytest.mark.django_db(transaction=True)
def test_department_scope_null_station_rejects_duplicate_attempt_number():
    _, department, scope, dataset_type_code = _context()
    DatasetPublication.objects.create(
        department=department,
        dataset_type_code=dataset_type_code,
        scope_state=scope,
        version_number=13,
        schema_version=1,
        source_revision=0,
        status=DatasetPublication.Status.FAILED,
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        DatasetPublication.objects.create(
            department=department,
            dataset_type_code=dataset_type_code,
            scope_state=scope,
            version_number=13,
            schema_version=1,
            source_revision=0,
            status=DatasetPublication.Status.OBSOLETE,
        )


@pytest.mark.django_db(transaction=True)
def test_station_scope_rejects_duplicate_attempt_number():
    _, department, scope, dataset_type_code = _context(station_scoped=True)
    DatasetPublication.objects.create(
        department=department,
        station=scope.station,
        dataset_type_code=dataset_type_code,
        scope_state=scope,
        version_number=13,
        schema_version=1,
        source_revision=0,
        status=DatasetPublication.Status.FAILED,
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        DatasetPublication.objects.create(
            department=department,
            station=scope.station,
            dataset_type_code=dataset_type_code,
            scope_state=scope,
            version_number=13,
            schema_version=1,
            source_revision=0,
            status=DatasetPublication.Status.REJECTED,
        )


@pytest.mark.django_db(transaction=True)
def test_competing_claimers_allocate_one_attempt_and_never_duplicate_its_version():
    admin, department, scope, dataset_type_code = _context()
    mark_dirty(actor=admin, department=department, dataset_type_code=dataset_type_code)
    PublicationJob.objects.filter(scope_state=scope).update(not_before=timezone.now())

    first = claim_next_job()
    second = claim_next_job()

    assert first is not None and first.build_publication is not None
    assert first.build_publication.version_number == 1
    assert second is None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'publications_datasetpublication'::regclass
              AND conname = 'unique_dataset_publication_version'
            """
        )
        row = cursor.fetchone()
    assert row is not None
    assert "UNIQUE NULLS NOT DISTINCT" in row[0]
