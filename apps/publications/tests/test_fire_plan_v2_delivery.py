import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.publications import artifacts
from apps.publications.builders import build_source_payload
from apps.publications.fire_plan_v2 import build_fire_plan_v2_generation
from apps.publications.fire_plan_v2_delivery import (
    build_fire_plan_v2_manifest,
    ensure_generation_key,
    generation_hpke_context,
    process_next_fire_plan_v2_generation_key_grant,
)
from apps.publications.hpke import (
    FirePlanGenerationHPKEContext,
    HPKEError,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    FirePlanGenerationKey,
    FirePlanGenerationKeyGrant,
)
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def v2_delivery_context(db, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    user = User.objects.create_user("v2-delivery@example.test", "V2 Delivery", "safe-password")
    department = Department.objects.create(name="V2 delivery", short_code="V2X", created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    tablet = Tablet.objects.create(
        department=department, display_name="Tablet", status=Tablet.Status.ACTIVE
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential = "v2-credential"
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite="DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=user
    )
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    kek_path, signing_path, ring_path = (
        tmp_path / "kek",
        tmp_path / "signing",
        tmp_path / "ring.json",
    )
    kek_path.write_bytes(b"k" * 32)
    signing_path.write_bytes(b"s" * 32)
    ring_path.write_text(
        json.dumps(
            {
                "keys": {
                    "1": base64.b64encode(
                        Ed25519PrivateKey.from_private_bytes(b"s" * 32)
                        .public_key()
                        .public_bytes_raw()
                    ).decode("ascii")
                }
            }
        ),
        encoding="ascii",
    )
    with override_settings(
        REFERENCE_DATA_ACCEPTED_ROOT=accepted,
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring_path,
    ):
        yield user, department, scope, accepted, installation, private_key, credential


def _plan(user, department, accepted, name, pdf):
    plan = FirePlan.objects.create(
        department=department,
        external_identifier=name,
        object_name=name,
        address=name,
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename=f"{name}.pdf",
        file_size=len(pdf),
        page_count=1,
        sha256=hashlib.sha256(pdf).hexdigest(),
        uploaded_by=user,
    )
    (accepted / plan.document_key).write_bytes(pdf)
    return plan


def _generation(department, scope, version):
    snapshot = build_source_payload(
        definition=get_dataset_definition("department_fire_plans"),
        department=department,
        station=None,
    )
    return DatasetPublication.objects.create(
        department=department,
        dataset_type_code="department_fire_plans",
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=version,
        source_snapshot=snapshot,
        status=DatasetPublication.Status.BUILDING,
    )


@pytest.mark.django_db(transaction=True)
def test_v2_manifest_grant_and_document_delivery(v2_delivery_context):
    user, department, scope, accepted, installation, private_key, credential = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    _plan(user, department, accepted, "B", b"PDF B")
    publication = _generation(department, scope, 1)
    references = build_fire_plan_v2_generation(publication=publication)
    manifest = build_fire_plan_v2_manifest(publication=publication)
    second_manifest = build_fire_plan_v2_manifest(publication=publication)
    assert manifest.payload == second_manifest.payload
    assert len(manifest.payload["documents"]) == 2
    Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
        bytes(manifest.signature),
        json.dumps(manifest.payload, sort_keys=True, separators=(",", ":")).encode("ascii"),
    )
    with pytest.raises(InvalidSignature):
        Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
            bytes(manifest.signature),
            json.dumps(
                {**manifest.payload, "version": 99}, sort_keys=True, separators=(",", ":")
            ).encode("ascii"),
        )
    protected_key = ensure_generation_key(publication=publication)
    assert len(protected_key.wrapped_key) == 40 and not hasattr(protected_key, "generation_key")
    from apps.publications.fire_plan_v2_delivery import request_fire_plan_v2_generation_key_grant

    request_fire_plan_v2_generation_key_grant(publication=publication, installation=installation)
    grant = process_next_fire_plan_v2_generation_key_grant()
    assert grant is not None and grant.status == FirePlanGenerationKeyGrant.Status.READY
    context = generation_hpke_context(publication=publication, installation=installation)
    generation_key = hpke_open(
        encapsulated_key=bytes(grant.hpke_encapsulated_key),
        ciphertext=bytes(grant.hpke_wrapped_generation_key),
        recipient_private_key=private_key,
        context=context,
    )
    with pytest.raises(HPKEError):
        hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_generation_key),
            recipient_private_key=private_key,
            context=FirePlanGenerationHPKEContext(**{**context.__dict__, "version_number": 2}),
        )
    with pytest.raises(HPKEError):
        hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_generation_key),
            recipient_private_key=private_key,
            context=FirePlanGenerationHPKEContext(
                **{**context.__dict__, "installation_id": uuid.uuid4()}
            ),
    )
    original_info = context.info()
    FirePlanGenerationKey.objects.filter(pk=protected_key.pk).update(
        wrapped_key=keywrap.aes_key_wrap(b"r" * 32, generation_key),
        kek_version="rewrapped-test-kek",
    )
    assert (
        generation_hpke_context(publication=publication, installation=installation).info()
        == original_info
    )
    document = manifest.payload["documents"][0]
    cek = keywrap.aes_key_unwrap(
        generation_key, base64.b64decode(document["generation_wrapped_cek"])
    )
    reference = next(
        ref for ref in references if str(ref.document_artifact_id) == document["artifact_id"]
    )
    ciphertext = (
        settings.PUBLICATION_ARTIFACT_ROOT / reference.document_artifact.artifact_path
    ).read_bytes()
    assert (
        AESGCM(cek)
        .decrypt(bytes(reference.document_artifact.nonce), ciphertext, None)
        .startswith(b"PDF")
    )
    with pytest.raises(InvalidTag):
        AESGCM(cek).decrypt(bytes(reference.document_artifact.nonce), ciphertext[:-1] + b"x", None)
    client = Client()
    path = (
        f"/api/v1/tablet/fire-plan-generations/{publication.id}/artifacts/"
        f"{reference.document_artifact_id}/download"
    )
    response = client.get(path, HTTP_AUTHORIZATION=f"Bearer {credential}")
    assert response.status_code == 200 and response["X-Accel-Redirect"].endswith(
        reference.document_artifact.artifact_path
    )
    manifest_response = client.get(
        f"/api/v1/tablet/fire-plan-generations/{publication.id}/manifest",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert manifest_response.status_code == 200
    assert (
        FirePlanGenerationHPKEContext.from_wire(
            manifest_response.json()["generation_key_grant"]["info"]
        ).info()
        == context.info()
    )
    assert (
        client.get(
            f"/api/v1/tablet/fire-plan-generations/{publication.id}/artifacts/{uuid.uuid4()}/download",
            HTTP_AUTHORIZATION=f"Bearer {credential}",
        ).status_code
        == 404
    )
