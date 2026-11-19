"""Current READY-artifact immutability and retained lazy signature upgrade."""

import hashlib
import uuid

import pytest
from django.db import DatabaseError
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department, Station
from apps.publications.artifacts import upgrade_legacy_scope_signature
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    LegacyArtifactScopeSignatureUpgrade,
)
from apps.publications.paths import publication_artifact_relative_path


@pytest.mark.django_db(transaction=True)
def test_current_scope_preserves_ready_artifacts_and_lazily_resigns_them(tmp_path):
    """Retain the live lazy-signature upgrade and immutable artifact regression."""
    user = User.objects.create_user("scope-upgrade@example.test", "Scope upgrade", "safe-password")
    department = Department.objects.create(name="Migration", short_code="MIG", created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    ciphertexts = {"department": b"department ciphertext", "station": b"station ciphertext"}
    old_publication_ids = {}
    for label, dataset_type_code, station_id in (
        ("department", "department_hydrants", None),
        ("station", "station_personnel", station.id),
    ):
        scope = DatasetScopeState.objects.create(
            scope_type="STATION" if station_id else "DEPARTMENT",
            department_id=department.id,
            station_id=station_id,
            dataset_type_code=dataset_type_code,
            source_revision=1,
        )
        publication_id = uuid.uuid4()
        old_publication_ids[label] = publication_id
        DatasetPublication.objects.create(
            id=publication_id,
            scope_type="STATION" if station_id else "DEPARTMENT",
            department_id=department.id,
            station_id=station_id,
            dataset_type_code=dataset_type_code,
            scope_state_id=scope.id,
            version_number=1,
            schema_version=1,
            source_revision=1,
            status="PUBLISHED",
            artifact_ready=True,
            artifact_status="READY",
            artifact_path=publication_artifact_relative_path(
                department_id=department.id, publication_id=publication_id
            ),
            artifact_size=len(ciphertexts[label]),
            artifact_sha256=hashlib.sha256(ciphertexts[label]).hexdigest(),
            artifact_nonce=b"n" * 12,
            artifact_wrapped_cek=b"w" * 40,
            artifact_encryption_algorithm="AES-256-GCM",
            artifact_wrapping_algorithm="AES-KW-RFC3394",
            artifact_kek_version="legacy-key",
            artifact_signature=b"l" * 64,
            artifact_signature_algorithm="Ed25519",
            artifact_signing_key_version="legacy-signing-key",
        )

    LegacyArtifactScopeSignatureUpgrade.objects.bulk_create(
        [
            LegacyArtifactScopeSignatureUpgrade(publication_id=pk)
            for pk in old_publication_ids.values()
        ]
    )
    publications = {
        label: DatasetPublication.objects.get(pk=publication_id)
        for label, publication_id in old_publication_ids.items()
    }
    assert publications["department"].scope_type == "DEPARTMENT"
    assert publications["station"].scope_type == "STATION"
    assert (
        LegacyArtifactScopeSignatureUpgrade.objects.filter(
            publication_id__in=old_publication_ids.values()
        ).count()
        == 2
    )
    for label, publication in publications.items():
        assert publication.artifact_path == publication_artifact_relative_path(
            department_id=department.id, publication_id=publication.id
        )
        assert publication.artifact_size == len(ciphertexts[label])
        assert publication.artifact_sha256 == hashlib.sha256(ciphertexts[label]).hexdigest()
        assert publication.artifact_signature == b"l" * 64
        assert publication.artifact_signature_algorithm == "Ed25519"
        assert publication.artifact_signing_key_version == "legacy-signing-key"

    with pytest.raises(DatabaseError, match="Ready publication artifact metadata is immutable"):
        DatasetPublication.objects.filter(pk=publications["department"].id).update(
            artifact_path="unexpected/artifact.bin"
        )

    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_SIGNING_KEY_VERSION="scope-upgrade",
    ):
        for label, publication in publications.items():
            artifact_path = tmp_path / publication.artifact_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(ciphertexts[label])
            assert upgrade_legacy_scope_signature(publication=publication) is True
            publication.refresh_from_db()
            assert publication.artifact_path == publication_artifact_relative_path(
                department_id=department.id, publication_id=publication.id
            )
            assert artifact_path.read_bytes() == ciphertexts[label]
            assert publication.artifact_signature != b"l" * 64
            assert publication.artifact_signature_algorithm == "Ed25519"
            assert publication.artifact_signing_key_version == "scope-upgrade"
            assert not LegacyArtifactScopeSignatureUpgrade.objects.filter(
                publication=publication
            ).exists()
            assert upgrade_legacy_scope_signature(publication=publication) is False
