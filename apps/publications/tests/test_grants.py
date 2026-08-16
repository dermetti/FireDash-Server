"""PostgreSQL-backed concurrency and lifecycle tests for DatasetKeyGrant and SignedManifest."""

import base64
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE, serialize_p256_public_key
from apps.publications.manifests import (
    authorized_publications,
    request_dataset_key_grant,
    request_manifest,
)
from apps.publications.models import (
    DatasetKeyGrant,
    DatasetPublication,
    DatasetScopeState,
    SignedManifest,
)
from apps.publications.work_cycle import process_delivery_cycle
from apps.publications.worker_grants import (
    claim_next_dataset_key_grant,
    claim_next_signed_manifest,
    process_next_dataset_key_grant,
    process_next_signed_manifest,
)
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def pub_fixture(db):
    now = timezone.now()
    user = User.objects.create_user("grants@example.test", "Grants User", "safe-password")
    department = Department.objects.create(name="Grants", short_code="GRT", created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Engine 1"
    )
    tablet = Tablet.objects.create(department=department, display_name="Tablet")
    private_key = ec.generate_private_key(ec.SECP256R1())
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash="a" * 64,
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=now, created_by=user
    )
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants", source_revision=1
    )
    cek, kek = b"c" * 32, b"k" * 32
    pub_id = uuid.uuid4()
    publication = DatasetPublication.objects.create(
        id=pub_id,
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=scope,
        version_number=7,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path=f"{department.id}/{pub_id}/artifact.bin",
        artifact_size=100,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=keywrap.aes_key_wrap(kek, cek),
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="1",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
    )
    return user, department, installation, publication, private_key, cek, kek


def test_dataset_key_grant_is_unique_per_installation_and_publication(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    grant = request_dataset_key_grant(publication=publication, installation=installation)
    assert grant.status == DatasetKeyGrant.Status.PENDING
    same_grant = request_dataset_key_grant(publication=publication, installation=installation)
    assert same_grant.id == grant.id


def test_dataset_key_grant_cannot_be_replayed(pub_fixture):
    """A publication/installation pair must have at most one non-revoked grant."""
    _, _, installation, publication, _, _, _ = pub_fixture
    grant1 = request_dataset_key_grant(publication=publication, installation=installation)
    grant1.delete()
    grant2 = request_dataset_key_grant(publication=publication, installation=installation)
    assert grant2.id != grant1.id


def test_skip_locked_prevents_double_claim(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    request_dataset_key_grant(publication=publication, installation=installation)
    first = claim_next_dataset_key_grant()
    second = claim_next_dataset_key_grant()
    assert first is not None
    assert second is None


def test_skip_locked_manifest_prevents_double_claim(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    request_manifest(installation=installation)
    first = claim_next_signed_manifest()
    second = claim_next_signed_manifest()
    assert first is not None
    assert second is None


def test_grant_becomes_ready_after_worker_processing(pub_fixture, tmp_path):
    _, _, installation, publication, _, cek, kek = pub_fixture
    request_dataset_key_grant(publication=publication, installation=installation)
    kek_path = tmp_path / "kek"
    kek_path.write_bytes(base64.b64encode(kek))
    with override_settings(PUBLICATION_KEK_CREDENTIAL_PATH=kek_path):
        grant = process_next_dataset_key_grant()
    assert grant is not None
    assert grant.status == DatasetKeyGrant.Status.READY
    assert grant.hpke_encapsulated_key is not None
    assert grant.hpke_wrapped_content_key is not None


def test_manifest_becomes_ready_after_worker_processing(pub_fixture, tmp_path):
    _, _, installation, publication, _, cek, kek = pub_fixture
    request_manifest(installation=installation)
    kek_path = tmp_path / "kek"
    kek_path.write_bytes(base64.b64encode(kek))
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    with override_settings(
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
    ):
        process_next_dataset_key_grant()
        signed = process_next_signed_manifest()
    assert signed is not None
    assert signed.status == SignedManifest.Status.READY
    assert signed.payload is not None


def test_failed_required_grant_fails_manifest_without_reclaiming_it(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    request_manifest(installation=installation)
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    grant.status = DatasetKeyGrant.Status.FAILED
    grant.completed_at = timezone.now()
    grant.error_message = "Publication KEK credential is unavailable."
    grant.save(update_fields=("status", "completed_at", "error_message"))

    manifest = process_next_signed_manifest()

    assert manifest is not None
    assert manifest.status == SignedManifest.Status.FAILED
    assert manifest.completed_at is not None
    assert manifest.error_message == "A required dataset key grant failed."
    assert process_next_signed_manifest() is None


def test_new_manifest_request_safely_retries_terminal_grant_and_manifest(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    first = request_manifest(installation=installation)
    assert first.request_id is not None
    DatasetKeyGrant.objects.filter(publication=publication, app_installation=installation).update(
        status=DatasetKeyGrant.Status.FAILED,
        completed_at=timezone.now(),
        error_message="Publication KEK credential is unavailable.",
    )
    SignedManifest.objects.filter(pk=first.request_id).update(
        status=SignedManifest.Status.FAILED,
        completed_at=timezone.now(),
        error_message="A required dataset key grant failed.",
    )

    retry = request_manifest(installation=installation)
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    manifest = SignedManifest.objects.get(pk=first.request_id)

    assert retry.request_id == first.request_id
    assert grant.status == DatasetKeyGrant.Status.PENDING
    assert grant.completed_at is None
    assert manifest.status == SignedManifest.Status.PENDING
    assert manifest.completed_at is None


def test_deferred_manifest_is_attempted_once_per_delivery_cycle_and_later_completes(
    pub_fixture, tmp_path
):
    _, _, installation, publication, _, _, _ = pub_fixture
    request_manifest(installation=installation)
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    grant.status = DatasetKeyGrant.Status.RUNNING
    grant.save(update_fields=("status",))

    deferred = process_delivery_cycle(batch_size=10)
    manifest = SignedManifest.objects.get(app_installation=installation)

    assert deferred.key_grants == 0
    assert deferred.manifests == 1
    assert deferred.deferred_manifests == 1
    assert deferred.forward_progress == 0
    assert manifest.status == SignedManifest.Status.PENDING

    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    grant.status = DatasetKeyGrant.Status.READY
    grant.hpke_ciphersuite = HPKE_CIPHERSUITE
    grant.hpke_encapsulated_key = b"e" * 65
    grant.hpke_wrapped_content_key = b"w" * 48
    grant.completed_at = timezone.now()
    grant.save(
        update_fields=(
            "status",
            "hpke_ciphersuite",
            "hpke_encapsulated_key",
            "hpke_wrapped_content_key",
            "completed_at",
        )
    )

    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        completed = process_delivery_cycle(batch_size=10)

    manifest.refresh_from_db()
    assert completed.forward_progress == 1
    assert manifest.status == SignedManifest.Status.READY


def test_revoked_installation_grants_are_revoked(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    request_dataset_key_grant(publication=publication, installation=installation)
    from apps.publications.manifests import revoke_dataset_key_grants

    count = revoke_dataset_key_grants(installation=installation)
    assert count == 1
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    assert grant.status == DatasetKeyGrant.Status.REVOKED


def test_stale_manifest_is_replaced_by_new_generation(pub_fixture, tmp_path):
    _, _, installation, publication, _, _, _ = pub_fixture
    first = request_manifest(installation=installation, generation=1)
    assert first.unavailable
    assert first.request_id is not None
    first_req = SignedManifest.objects.get(pk=first.request_id)
    first_req.status = SignedManifest.Status.OBSOLETE
    first_req.save(update_fields=("status",))

    second = request_manifest(installation=installation, generation=2)
    assert second.unavailable
    assert second.request_id != first.request_id


def test_download_requires_authorized_publication(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    _, vehicle, pubs = authorized_publications(installation=installation)
    assert any(p.id == publication.id for p in pubs)


def test_download_denied_when_no_vehicle_assignment(pub_fixture):
    _, _, installation, publication, _, _, _ = pub_fixture
    TabletVehicleAssignment.objects.filter(tablet=installation.tablet).update(
        ended_at=timezone.now()
    )
    from apps.publications.manifests import ManifestError

    with pytest.raises(ManifestError, match="vehicle"):
        authorized_publications(installation=installation)
