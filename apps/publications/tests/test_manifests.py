import base64
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKEContext, HPKEError, hpke_open, serialize_p256_public_key
from apps.publications.manifests import canonical_manifest_payload, request_manifest
from apps.publications.models import (
    DatasetKeyGrant,
    DatasetPublication,
    DatasetScopeState,
    SignedManifest,
)
from apps.publications.worker_grants import (
    process_next_dataset_key_grant,
    process_next_signed_manifest,
    sign_manifest_payload,
)
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def grant_context(db):
    now = timezone.now()
    user = User.objects.create_user("manifest@example.test", "Manifest User", "safe-password")
    department = Department.objects.create(name="Manifest", short_code="MAN", created_by=user)
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
        app_version="1.0",
        hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite="DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
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
    publication = DatasetPublication.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=scope,
        version_number=7,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path="manifest/artifact.bin",
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
    return installation, publication, private_key, cek, kek


def test_web_manifest_queues_grant_without_reading_kek_and_worker_fulfills_it(
    grant_context, tmp_path
):
    installation, publication, private_key, cek, kek = grant_context

    pending = request_manifest(installation=installation)
    assert pending.unavailable and pending.payload is None
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    assert grant.status == DatasetKeyGrant.Status.PENDING

    kek_path = tmp_path / "kek"
    kek_path.write_bytes(base64.b64encode(kek))
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    with override_settings(
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
    ):
        grant = process_next_dataset_key_grant()
        signed_manifest = process_next_signed_manifest()
    assert grant is not None and grant.status == DatasetKeyGrant.Status.READY
    assert signed_manifest is not None and signed_manifest.status == SignedManifest.Status.READY
    Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
        bytes(signed_manifest.signature), canonical_manifest_payload(signed_manifest.payload)
    )

    result = request_manifest(installation=installation)
    assert not result.unavailable and result.payload is not None
    assert result.payload["signature_algorithm"] == "Ed25519"
    assert result.payload["signing_key_version"] == "1"
    context = HPKEContext(
        publication_id=publication.id,
        installation_id=installation.id,
        tablet_id=installation.tablet_id,
        department_id=publication.department_id,
        station_id=None,
        dataset_type_code=publication.dataset_type_code,
        version_number=publication.version_number,
        schema_version=publication.schema_version,
        ciphertext_sha256=publication.artifact_sha256,
    )
    assert (
        hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_content_key),
            recipient_private_key=private_key,
            context=context,
        )
        == cek
    )
    with pytest.raises(HPKEError, match="authentication failed"):
        hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_content_key),
            recipient_private_key=private_key,
            context=HPKEContext(**{**context.__dict__, "version_number": 8}),
        )


def test_manifest_requests_coalesce_by_installation_and_current_state(grant_context):
    installation, _, _, _, _ = grant_context

    first = request_manifest(installation=installation)
    second = request_manifest(installation=installation)

    assert first.unavailable and second.unavailable
    assert first.request_id == second.request_id
    assert SignedManifest.objects.count() == 1


def test_worker_manifest_signature_covers_canonical_payload(tmp_path):
    signing_seed = b"s" * 32
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(signing_seed)
    payload = {"datasets": [], "manifest_generation": 1}

    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        signature = sign_manifest_payload(payload=payload)

    Ed25519PrivateKey.from_private_bytes(signing_seed).public_key().verify(
        signature, canonical_manifest_payload(payload)
    )
