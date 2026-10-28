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
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications import artifacts
from apps.publications.builders import build_source_payload
from apps.publications.document_v2 import (
    build_document_v2_generation,
    build_document_v2_manifest,
    ensure_generation_key,
    generation_hpke_context,
    process_next_generation_key_grant,
)
from apps.publications.hpke import (
    DocumentGenerationHPKEContext,
    HPKEError,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    DocumentGenerationKeyGrant,
)
from apps.publications.registry import get_dataset_definition
from apps.publications.services import (
    enqueue_publication_job,
    process_next_job,
)
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
        schema_version=2,
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
    references = build_document_v2_generation(publication=publication)
    manifest = build_document_v2_manifest(publication=publication)
    second_manifest = build_document_v2_manifest(publication=publication)
    assert manifest.payload == second_manifest.payload
    assert len(manifest.payload["documents"]) == 2
    assert manifest.payload["format"] == "document-generation-v2"
    assert manifest.payload["dataset_type"] == "department_fire_plans"
    assert manifest.payload["schema_version"] == 2
    assert all("path" not in d["fire_plan"] for d in manifest.payload["documents"])
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
    # A built candidate is not entitled for delivery until explicitly published.
    publication.status = publication.Status.PUBLISHED
    publication.artifact_ready = True
    publication.artifact_status = publication.ArtifactStatus.READY
    publication.published_at = timezone.now()
    publication.save()
    scope.current_published_publication = publication
    scope.latest_built_publication = publication
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))
    from apps.publications.document_v2 import request_generation_key_grant

    request_generation_key_grant(publication=publication, installation=installation)
    pending = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/manifest",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert pending.status_code == 202 and pending.content == b""
    assert pending["Retry-After"] == "5"
    grant = process_next_generation_key_grant()
    assert grant is not None and grant.status == DocumentGenerationKeyGrant.Status.READY
    context = generation_hpke_context(publication=publication, installation=installation)
    assert context.wire()["protocol"] == "firedash-document-generation-hpke-v2"
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
            context=DocumentGenerationHPKEContext(**{**context.__dict__, "version_number": 2}),
        )
    with pytest.raises(HPKEError):
        hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_generation_key),
            recipient_private_key=private_key,
            context=DocumentGenerationHPKEContext(
                **{**context.__dict__, "installation_id": uuid.uuid4()}
            ),
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
    assert hashlib.sha256(ciphertext).hexdigest() == document["ciphertext_sha256"]
    assert len(ciphertext) == document["ciphertext_size"]
    assert (
        AESGCM(cek)
        .decrypt(bytes(reference.document_artifact.nonce), ciphertext, None)
        .startswith(b"PDF")
    )
    with pytest.raises(InvalidTag):
        AESGCM(cek).decrypt(
            bytes(reference.document_artifact.nonce),
            ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]),
            None,
        )
    client = Client()
    path = (
        f"/api/v1/tablet/document-generations/{publication.id}/artifacts/"
        f"{reference.document_artifact_id}/download"
    )
    response = client.get(path, HTTP_AUTHORIZATION=f"Bearer {credential}")
    assert response.status_code == 200 and response["X-Accel-Redirect"].endswith(
        reference.document_artifact.artifact_path
    )
    manifest_response = client.get(
        f"/api/v1/tablet/document-generations/{publication.id}/manifest",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert manifest_response.status_code == 200
    conditional = client.get(
        path, HTTP_AUTHORIZATION=f"Bearer {credential}", HTTP_IF_NONE_MATCH=response["ETag"]
    )
    assert conditional.status_code == 304 and conditional.content == b""
    assert (
        client.get(
            path, HTTP_AUTHORIZATION=f"Bearer {credential}", HTTP_IF_NONE_MATCH="*"
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/tablet/document-generations/{publication.id}/manifest",
            HTTP_AUTHORIZATION=f"Bearer {credential}",
            HTTP_IF_NONE_MATCH="*",
        ).status_code
        == 200
    )
    from apps.publications.manifests import _publication_manifest_entry

    assert _publication_manifest_entry(publication)["manifest_url"] == (
        f"/api/v1/tablet/document-generations/{publication.id}/manifest"
    )
    assert (
        DocumentGenerationHPKEContext.from_wire(
            manifest_response.json()["generation_key_grant"]["info"]
        ).info()
        == context.info()
    )
    assert (
        client.get(
            f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{uuid.uuid4()}/download",
            HTTP_AUTHORIZATION=f"Bearer {credential}",
        ).status_code
        == 404
    )


@pytest.mark.django_db(transaction=True)
def test_normal_worker_builds_generic_generation_without_companion(
    v2_delivery_context, monkeypatch
):
    def forbidden(**kwargs):
        pytest.fail("Generic Fire Plans must not build legacy companion artifacts")

    monkeypatch.setattr("apps.publications.services.build_artifact", forbidden)
    monkeypatch.setattr("apps.publications.services.build_encrypted_artifact", forbidden)
    user, department, scope, accepted, _, _, _ = v2_delivery_context
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    _plan(user, department, accepted, "Canonical", b"PDF canonical")

    enqueue_publication_job(
        department=department,
        station=None,
        dataset_type_code="department_fire_plans",
        requested_by=user,
        trigger_type="USER_REQUEST",
    )

    job = process_next_job()
    assert job is not None and job.status == job.Status.SUCCEEDED
    assert job.build_publication_id is not None
    publication = DatasetPublication.objects.get(pk=job.build_publication_id)
    assert publication.status == DatasetPublication.Status.PUBLISHED
    assert publication.schema_version == 2
    assert publication.document_artifact_references.count() == 1
    assert hasattr(publication, "document_generation_manifest")
    assert publication.artifact_path == ""
    assert publication.artifact_size is None
    assert not hasattr(publication, "fire_plan_generation_manifest")
    assert not hasattr(publication, "fire_plan_artifact_references")
    assert not (
        settings.PUBLICATION_ARTIFACT_ROOT / str(department.id) / str(publication.id)
    ).exists()


def _publish_generic(department, scope, version=1):
    publication = _generation(department, scope, version)
    build_document_v2_generation(publication=publication)
    build_document_v2_manifest(publication=publication)
    publication.status = publication.Status.PUBLISHED
    publication.artifact_status = publication.ArtifactStatus.READY
    publication.artifact_ready = True
    publication.published_at = timezone.now()
    publication.clean()
    publication.save()
    scope.current_published_publication = publication
    scope.latest_built_publication = publication
    scope.save(update_fields=("current_published_publication", "latest_built_publication"))
    return publication


@pytest.mark.django_db(transaction=True)
def test_empty_generation_and_registry(v2_delivery_context):
    from apps.publications.models import DatasetTypeRegistry

    _, department, scope, _, installation, _, credential = v2_delivery_context
    publication = _publish_generic(department, scope)
    assert publication.document_generation_manifest.payload["documents"] == []
    registry = DatasetTypeRegistry.objects.get(code="department_fire_plans")
    assert registry.current_schema_version == 2 and registry.supported_schema_versions == [2]
    client = Client()
    path = f"/api/v1/tablet/document-generations/{publication.id}/manifest"
    assert client.get(path, HTTP_AUTHORIZATION=f"Bearer {credential}").status_code == 202
    grant = process_next_generation_key_grant()
    assert grant.status == grant.Status.READY and grant.app_installation_id == installation.id
    assert client.get(path, HTTP_AUTHORIZATION=f"Bearer {credential}").json()["documents"] == []
    assert process_next_generation_key_grant() is None
    assert DocumentGenerationKeyGrant.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "loss",
    [
        "lease",
        "installation",
        "assignment",
        "feature",
        "department",
        "unpublished",
        "scope_pointer",
    ],
)
def test_authorization_loss_revokes_pending_grant(v2_delivery_context, loss):
    from apps.publications.document_v2 import request_generation_key_grant
    from apps.publications.feature_services import set_department_feature

    user, department, scope, accepted, installation, _, credential = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    request_generation_key_grant(publication=publication, installation=installation)
    if loss == "lease":
        AppInstallation.objects.filter(pk=installation.pk).update(
            authorization_valid_until=timezone.now()
        )
    elif loss == "installation":
        AppInstallation.objects.filter(pk=installation.pk).update(status="REVOKED")
    elif loss == "assignment":
        TabletVehicleAssignment.objects.filter(tablet=installation.tablet).update(
            valid_until=timezone.now()
        )
    elif loss == "feature":
        DepartmentMembership.objects.create(user=user, department=department, created_by=user)
        set_department_feature(
            actor=user, department=department, feature_code="publications", enabled=False
        )
    elif loss == "department":
        other = Department.objects.create(name="Other", short_code="OTH", created_by=user)
        Tablet.objects.filter(pk=installation.tablet_id).update(department=other)
    elif loss == "scope_pointer":
        DatasetScopeState.objects.filter(pk=scope.pk).update(current_published_publication=None)
    else:
        DatasetPublication.objects.filter(pk=publication.pk).update(status="SUPERSEDED")
    grant = process_next_generation_key_grant()
    assert grant.status == grant.Status.REVOKED and grant.hpke_wrapped_generation_key is None
    artifact_id = publication.document_artifact_references.get().document_artifact_id
    response = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact_id}/download",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert response.status_code in (401, 403)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("grant_status", ["PENDING", "RUNNING", "FAILED", "REVOKED"])
def test_nonready_grant_cannot_download(v2_delivery_context, grant_status):
    user, department, scope, accepted, installation, _, credential = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    DocumentGenerationKeyGrant.objects.create(
        publication=publication, app_installation=installation, status=grant_status
    )
    artifact_id = publication.document_artifact_references.get().document_artifact_id
    response = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact_id}/download",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert response.status_code == 403
    manifest = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/manifest",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert manifest.status_code == 202 and manifest.content == b""
    stored = DocumentGenerationKeyGrant.objects.get(publication=publication)
    assert stored.status in ("PENDING", "RUNNING")
    assert stored.hpke_wrapped_generation_key is None


@pytest.mark.django_db(transaction=True)
def test_generic_grant_recovers_running_and_binds_installation(v2_delivery_context):
    user, department, scope, accepted, installation, _, _ = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    grant = DocumentGenerationKeyGrant.objects.create(
        publication=publication, app_installation=installation, status="RUNNING"
    )
    completed = process_next_generation_key_grant()
    assert completed.id == grant.id and completed.status == "READY"
    assert process_next_generation_key_grant() is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("incomplete", ["key", "manifest", "references", "status"])
def test_postgresql_ready_guard_rejects_incomplete_generation(v2_delivery_context, incomplete):
    from django.db import DatabaseError, connection, transaction

    from apps.publications.models import DocumentGenerationKey, DocumentGenerationManifest

    assert connection.vendor == "postgresql"
    user, department, scope, accepted, _, _, _ = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _generation(department, scope, 1)
    if incomplete != "references":
        build_document_v2_generation(publication=publication)
    if incomplete != "references":
        build_document_v2_manifest(publication=publication)
    if incomplete == "key":
        DocumentGenerationKey.objects.filter(publication=publication).delete()
    elif incomplete == "manifest":
        DocumentGenerationManifest.objects.filter(publication=publication).delete()
    with pytest.raises(DatabaseError, match="complete ready document generation"):
        with transaction.atomic():
            DatasetPublication.objects.filter(pk=publication.pk).update(
                status="PUBLISHED",
                artifact_status="PENDING" if incomplete == "status" else "READY",
                artifact_ready=True,
            )


@pytest.mark.django_db(transaction=True)
def test_postgresql_generic_crypto_and_membership_are_immutable(v2_delivery_context):
    from django.db import DatabaseError, transaction

    from apps.publications.models import DocumentArtifact, DocumentGenerationManifest

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    with pytest.raises(DatabaseError, match="immutable"):
        with transaction.atomic():
            DocumentGenerationManifest.objects.filter(publication=publication).update(payload={})
    with pytest.raises(DatabaseError, match="immutable"):
        with transaction.atomic():
            DocumentArtifact.objects.update(ciphertext_sha256="f" * 64)
    with pytest.raises(DatabaseError, match="protected"):
        with transaction.atomic():
            publication.document_artifact_references.all().delete()
    with pytest.raises(DatabaseError, match="protected"):
        with transaction.atomic():
            DocumentGenerationManifest.objects.filter(publication=publication).delete()
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            DatasetPublication.objects.filter(pk=publication.pk).update(schema_version=1)
    publication.refresh_from_db()
    assert publication.schema_version == 2


@pytest.mark.django_db(transaction=True)
def test_delivery_lane_uses_only_generic_fire_plan_grants(v2_delivery_context, monkeypatch):
    from apps.publications.document_v2 import request_generation_key_grant
    from apps.publications.work_cycle import process_delivery_cycle

    _, department, scope, _, installation, _, _ = v2_delivery_context
    publication = _publish_generic(department, scope)
    request_generation_key_grant(publication=publication, installation=installation)

    result = process_delivery_cycle(batch_size=1)
    assert result.key_grants == 1
    assert DocumentGenerationKeyGrant.objects.get(publication=publication).status == "READY"


@pytest.mark.django_db(transaction=True)
def test_ready_grant_for_another_installation_does_not_authorize_download(v2_delivery_context):
    from copy import copy

    from apps.publications.document_v2 import request_generation_key_grant

    user, department, scope, accepted, installation, _, credential = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    request_generation_key_grant(publication=publication, installation=installation)
    assert process_next_generation_key_grant().status == "READY"
    tablet = Tablet.objects.create(department=department, display_name="Other", status="ACTIVE")
    other = copy(installation)
    other.pk = uuid.uuid4()
    other.installation_uuid = uuid.uuid4()
    other.tablet = tablet
    other.credential_hash = hmac.new(
        settings.SECRET_KEY.encode(), b"other-credential", hashlib.sha256
    ).hexdigest()
    other.save(force_insert=True)
    vehicle = TabletVehicleAssignment.objects.get(tablet=installation.tablet).vehicle
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=user
    )
    artifact_id = publication.document_artifact_references.get().document_artifact_id
    response = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact_id}/download",
        HTTP_AUTHORIZATION="Bearer other-credential",
    )
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_generic_grant_workers_skip_locked_incomplete_state(v2_delivery_context, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    from apps.publications import document_v2

    _, department, scope, _, installation, _, _ = v2_delivery_context
    publication = _publish_generic(department, scope)
    grant = document_v2.request_generation_key_grant(
        publication=publication, installation=installation
    )
    entered, release = Event(), Event()
    seal = document_v2.hpke_seal

    def paused_seal(**kwargs):
        entered.set()
        assert release.wait(timeout=15)
        return seal(**kwargs)

    monkeypatch.setattr(document_v2, "hpke_seal", paused_seal)

    def worker():
        close_old_connections()
        try:
            return document_v2.process_next_generation_key_grant()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(timeout=15)
            assert document_v2.process_next_generation_key_grant() is None
            grant.refresh_from_db()
            assert grant.status == "PENDING" and grant.hpke_wrapped_generation_key is None
        finally:
            release.set()
        assert future.result(timeout=15).status == "READY"
    assert DocumentGenerationKeyGrant.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_authorization_loss_during_hpke_does_not_finish_ready(v2_delivery_context, monkeypatch):
    from apps.publications import document_v2

    _, department, scope, _, installation, _, _ = v2_delivery_context
    publication = _publish_generic(department, scope)
    document_v2.request_generation_key_grant(publication=publication, installation=installation)
    seal = document_v2.hpke_seal

    def revoke_after_seal(**kwargs):
        result = seal(**kwargs)
        AppInstallation.objects.filter(pk=installation.pk).update(status="REVOKED")
        return result

    monkeypatch.setattr(document_v2, "hpke_seal", revoke_after_seal)
    grant = document_v2.process_next_generation_key_grant()
    assert grant.status == "REVOKED" and grant.hpke_wrapped_generation_key is None


@pytest.mark.django_db(transaction=True)
def test_generic_endpoints_enforce_app_version(v2_delivery_context):
    from apps.authorization.models import ApiVersionCompatibilityPolicy

    user, department, scope, _, _, _, credential = v2_delivery_context
    publication = _publish_generic(department, scope)
    ApiVersionCompatibilityPolicy.objects.update_or_create(
        api_major=1, defaults={"minimum_app_version": "9.0.0", "updated_by": user}
    )
    response = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/manifest",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert response.status_code == 426
    assert not DocumentGenerationKeyGrant.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failure", ["pdf", "signing", "signing_new", "missing", "truncated", "corrupted"]
)
def test_generic_fire_plan_worker_failure_preserves_current_publication(
    v2_delivery_context, monkeypatch, failure
):
    from apps.publications.worker_grants import KeyGrantError

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    plan = _plan(user, department, accepted, "A", b"PDF A")
    current = _publish_generic(department, scope)
    if failure == "pdf":
        (accepted / plan.document_key).unlink()
    elif failure in {"missing", "truncated", "corrupted"}:
        artifact = current.document_artifact_references.get().document_artifact
        path = settings.PUBLICATION_ARTIFACT_ROOT / artifact.artifact_path
        ciphertext = path.read_bytes()
        if failure == "missing":
            path.unlink()
        elif failure == "truncated":
            path.write_bytes(ciphertext[:-1])
        else:
            path.write_bytes(bytes([ciphertext[0] ^ 1]) + ciphertext[1:])
    else:
        if failure == "signing_new":
            _plan(user, department, accepted, "B", b"PDF B")

        def fail_signature(**kwargs):
            raise KeyGrantError("Signing credential unavailable")

        monkeypatch.setattr("apps.publications.document_v2.sign_manifest_payload", fail_signature)
    enqueue_publication_job(
        department=department,
        station=None,
        dataset_type_code="department_fire_plans",
        requested_by=user,
        trigger_type="USER_REQUEST",
        allow_clean_rebuild=True,
    )
    job = process_next_job()
    assert job.status == "FAILED"
    failed = job.build_publication
    assert failed.status == "FAILED" and failed.artifact_path == ""
    assert not failed.document_artifact_references.exists()
    scope.refresh_from_db()
    assert scope.current_published_publication_id == current.id
    assert current.document_artifact_references.count() == 1
    if failure == "signing_new":
        from apps.publications.models import DocumentArtifact, DocumentArtifactCleanup

        assert DocumentArtifact.objects.count() == 1
        assert not DocumentArtifactCleanup.objects.exists()
        assert (
            len(list((settings.PUBLICATION_ARTIFACT_ROOT / "documents").glob("*/artifact.bin")))
            == 1
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("optional", [False, True])
def test_signed_generic_manifest_preserves_frozen_fire_plan_metadata(v2_delivery_context, optional):
    from django.contrib.gis.geos import Point

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    plan = _plan(user, department, accepted, "A", b"PDF A")
    plan.fsd_location = "FSD entrance" if optional else ""
    plan.bmz_location = "BMZ upstairs" if optional else ""
    plan.rwa_info = "RWA manual" if optional else ""
    plan.location = Point(10.09873774, 53.59229519, srid=4326) if optional else None
    plan.save(update_fields=("fsd_location", "bmz_location", "rwa_info", "location"))
    publication = _publish_generic(department, scope)
    metadata = publication.document_generation_manifest.payload["documents"][0]["fire_plan"]
    assert metadata == publication.source_snapshot["fire_plans"][0]
    assert "path" not in metadata
    assert metadata["fsd_location"] == ("FSD entrance" if optional else None)
    assert metadata["bmz_location"] == ("BMZ upstairs" if optional else None)
    assert metadata["rwa_info"] == ("RWA manual" if optional else None)
    assert metadata["longitude"] == (10.09873774 if optional else None)
    assert metadata["latitude"] == (53.59229519 if optional else None)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "field,value",
    [
        ("generation_wrapped_cek", None),
        ("generation_wrapped_cek", 123),
        ("generation_wrapped_cek", []),
        ("generation_wrapped_cek", {}),
        ("generation_wrapped_cek", "invalid"),
        ("ciphertext_size", "123"),
        ("nonce", None),
        ("signature", []),
        ("encryption_algorithm", "wrong"),
    ],
)
def test_malformed_stored_document_metadata_fails_validation(v2_delivery_context, field, value):
    from copy import deepcopy

    from django.db import DatabaseError, transaction

    from apps.publications.document_v2 import DocumentV2Error, validate_ready_document_generation
    from apps.publications.models import DocumentGenerationManifest

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _generation(department, scope, 1)
    build_document_v2_generation(publication=publication)
    manifest = build_document_v2_manifest(publication=publication)
    payload = deepcopy(manifest.payload)
    payload["documents"][0][field] = value
    manifest.delete()  # BUILDING generation: replace only in this negative test.
    DocumentGenerationManifest.objects.create(
        publication=publication,
        payload=payload,
        signature=b"s" * 64,
        signature_algorithm="Ed25519",
        signing_key_version="test",
    )
    with pytest.raises(DocumentV2Error):
        validate_ready_document_generation(publication)
    with pytest.raises(DatabaseError, match="complete ready document"):
        with transaction.atomic():
            DatasetPublication.objects.filter(pk=publication.pk).update(
                status="PUBLISHED", artifact_status="READY", artifact_ready=True
            )


def test_obsolete_routes_and_schema_entries_are_absent():
    from django.urls import NoReverseMatch, Resolver404, resolve, reverse
    from drf_spectacular.generators import SchemaGenerator

    publication_id, artifact_id = uuid.uuid4(), uuid.uuid4()
    for path in (
        f"/api/v1/tablet/fire-plan-generations/{publication_id}/manifest",
        f"/api/v1/tablet/fire-plan-generations/{publication_id}/artifacts/{artifact_id}/download",
    ):
        with pytest.raises(Resolver404):
            resolve(path)
    with pytest.raises(NoReverseMatch):
        reverse("publications-scope-fire-plan-document-manifest-cutover", args=(publication_id,))
    schema = SchemaGenerator().get_schema(request=None, public=True)
    assert not any("fire-plan-generations" in path for path in schema["paths"])
    assert resolve(f"/api/v1/tablet/document-generations/{publication_id}/manifest")


@pytest.mark.django_db(transaction=True)
def test_authorization_precedes_matching_artifact_etag(v2_delivery_context):
    from apps.publications.document_v2 import request_generation_key_grant

    user, department, scope, accepted, installation, _, credential = v2_delivery_context
    _plan(user, department, accepted, "A", b"PDF A")
    publication = _publish_generic(department, scope)
    request_generation_key_grant(publication=publication, installation=installation)
    process_next_generation_key_grant()
    artifact = publication.document_artifact_references.get().document_artifact
    grant = DocumentGenerationKeyGrant.objects.get(publication=publication)
    grant.status = "REVOKED"
    grant.save(update_fields=("status",))
    response = Client().get(
        f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{artifact.id}/download",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
        HTTP_IF_NONE_MATCH=f'"{artifact.ciphertext_sha256}"',
    )
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_document_descriptor_rejects_missing_generation_and_obsolete_schema(v2_delivery_context):
    from apps.publications.manifests import ManifestError, _publication_manifest_entry

    _, department, scope, _, _, _, _ = v2_delivery_context
    publication = _generation(department, scope, 1)
    with pytest.raises(ManifestError, match="complete generic"):
        _publication_manifest_entry(publication)
    publication.schema_version = 1
    with pytest.raises(ManifestError, match="complete generic"):
        _publication_manifest_entry(publication)


@pytest.mark.django_db(transaction=True)
def test_restore_valid_generic_generation_after_pdf_replacement(v2_delivery_context):
    from apps.publications.services import rollback_publication

    user, department, scope, accepted, installation, _, credential = v2_delivery_context
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    plan = _plan(user, department, accepted, "A", b"PDF A")
    first = _publish_generic(department, scope)
    original = first.document_artifact_references.get().document_artifact
    first.status = "SUPERSEDED"
    first.save(update_fields=("status",))
    replacement = b"PDF replacement"
    plan.sha256 = hashlib.sha256(replacement).hexdigest()
    plan.file_size = len(replacement)
    plan.save(update_fields=("sha256", "file_size"))
    (accepted / plan.document_key).write_bytes(replacement)
    second = _publish_generic(department, scope, version=2)
    assert second.document_artifact_references.get().document_artifact_id != original.id
    rollback_publication(actor=user, publication=first)
    scope.refresh_from_db()
    second.refresh_from_db()
    assert scope.current_published_publication_id == first.id
    assert second.status == "SUPERSEDED"
    from apps.publications.document_v2 import request_generation_key_grant

    request_generation_key_grant(publication=first, installation=installation)
    process_next_generation_key_grant()
    response = Client().get(
        f"/api/v1/tablet/document-generations/{first.id}/artifacts/{original.id}/download",
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "code", ["department_fire_plans", "department_klgv_plans", "department_files", "station_files"]
)
@pytest.mark.parametrize("fault", ["missing", "legacy", "metadata", "references", "snapshot"])
def test_incomplete_historical_generation_never_activates(v2_delivery_context, client, code, fault):
    import time

    from django.urls import reverse

    from apps.publications.models import PublicationActivation
    from apps.publications.services import (
        PublicationError,
        delete_publication,
        rollback_publication,
    )

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    if code != "department_fire_plans":
        scope = DatasetScopeState.objects.create(
            department=department,
            dataset_type_code=code,
            scope_type="STATION" if code == "station_files" else "DEPARTMENT",
            station=Station.objects.get(department=department) if code == "station_files" else None,
        )
    # Historical broken rows can still say READY/SUPERSEDED. They must be
    # rejected before any activation or database readiness trigger is reached.
    snapshot = build_source_payload(
        definition=get_dataset_definition(code), department=department, station=scope.station
    )
    old = DatasetPublication.objects.create(
        department=department,
        station=scope.station,
        scope_type=scope.scope_type,
        dataset_type_code=code,
        scope_state=scope,
        version_number=11,
        schema_version=1 if fault == "legacy" else 2,
        source_revision=1,
        source_snapshot=(
            {
                **snapshot,
                {"department_fire_plans": "fire_plans", "department_klgv_plans": "klgv_plans"}.get(
                    code, "documents"
                ): [{"id": str(uuid.uuid4())}],
            }
            if fault == "snapshot"
            else snapshot
        ),
        status="SUPERSEDED",
        artifact_ready=True,
        artifact_status="READY",
    )
    if fault in {"metadata", "references"}:
        # No immutable READY generation is edited: construct malformed historical
        # input before finalizing it, as a pre-migration database could contain.
        from apps.publications.models import DocumentGenerationKey, DocumentGenerationManifest

        DocumentGenerationKey.objects.create(
            publication=old,
            wrapped_key=b"k" * 40,
            wrapping_algorithm="AES-KW-RFC3394",
            kek_version="test",
        )
        DocumentGenerationManifest.objects.create(
            publication=old,
            signature=b"s" * 64,
            signature_algorithm="Ed25519",
            signing_key_version="test",
            payload={
                "format": "document-generation-v2",
                "dataset_type": code,
                "publication_id": str(old.id),
                "version": old.version_number,
                "schema_version": 2,
                "documents": [{"artifact_id": str(uuid.uuid4())}]
                if fault == "references"
                else "invalid",
            },
        )
    # The authoritative pointer is a separate valid generation.
    current = DatasetPublication.objects.create(
        department=department,
        station=scope.station,
        scope_type=scope.scope_type,
        dataset_type_code=code,
        scope_state=scope,
        version_number=14,
        schema_version=2,
        source_revision=1,
        source_snapshot=snapshot,
        status="BUILDING",
    )
    build_document_v2_generation(publication=current)
    build_document_v2_manifest(publication=current)
    current.status, current.artifact_status, current.artifact_ready = "PUBLISHED", "READY", True
    current.save()
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()
    page = client.get(reverse("publications-scope-detail", args=(scope.id,)))
    assert page.status_code == 200
    assert not page.context["history_page"].object_list[1].can_rollback
    assert (
        "Incomplete document generation" in page.content.decode()
        or "legacy document format" in page.content.decode()
    )
    assert (
        reverse("publications-lifecycle-modal", args=(old.id, "rollback"))
        not in page.content.decode()
    )
    assert "Danger zone" not in page.content.decode()
    before = PublicationActivation.objects.count()
    response = client.post(reverse("publications-lifecycle-modal", args=(old.id, "rollback")))
    assert response.status_code == 200
    with pytest.raises(PublicationError):
        rollback_publication(actor=user, publication=old)
    with pytest.raises(PublicationError, match="no safe predecessor"):
        delete_publication(actor=user, publication=current)
    scope.refresh_from_db()
    old.refresh_from_db()
    current.refresh_from_db()
    assert scope.current_published_publication_id == current.id
    assert old.status == "SUPERSEDED" and current.status == "PUBLISHED"
    assert PublicationActivation.objects.count() == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "code", ["department_fire_plans", "department_klgv_plans", "department_files", "station_files"]
)
@pytest.mark.parametrize("operation", ["rollback", "delete"])
def test_complete_empty_historical_generation_can_restore(v2_delivery_context, code, operation):
    from apps.publications.services import rollback_publication

    user, department, scope, _, _, _, _ = v2_delivery_context
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    if code != "department_fire_plans":
        scope = DatasetScopeState.objects.create(
            department=department,
            dataset_type_code=code,
            scope_type="STATION" if code == "station_files" else "DEPARTMENT",
            station=Station.objects.get(department=department) if code == "station_files" else None,
        )
    versions = []
    for version in (1, 3):
        publication = DatasetPublication.objects.create(
            department=department,
            station=scope.station,
            scope_type=scope.scope_type,
            dataset_type_code=code,
            scope_state=scope,
            version_number=version,
            schema_version=2,
            source_revision=1,
            status="BUILDING",
            source_snapshot=build_source_payload(
                definition=get_dataset_definition(code),
                department=department,
                station=scope.station,
            ),
        )
        build_document_v2_generation(publication=publication)
        build_document_v2_manifest(publication=publication)
        publication.status, publication.artifact_status, publication.artifact_ready = (
            "SUPERSEDED" if version == 1 else "PUBLISHED",
            "READY",
            True,
        )
        publication.save()
        versions.append(publication)
    scope.current_published_publication = versions[1]
    scope.save(update_fields=("current_published_publication",))
    if operation == "delete":
        from apps.publications.services import delete_publication

        DatasetPublication.objects.create(
            department=department,
            station=scope.station,
            scope_type=scope.scope_type,
            dataset_type_code=code,
            scope_state=scope,
            version_number=2,
            schema_version=2,
            source_revision=1,
            source_snapshot=versions[0].source_snapshot,
            status="SUPERSEDED",
            artifact_ready=True,
            artifact_status="READY",
        )
        delete_publication(actor=user, publication=versions[1])
    else:
        rollback_publication(actor=user, publication=versions[0])
    scope.refresh_from_db()
    assert scope.current_published_publication_id == versions[0].id
    assert versions[0].document_artifact_references.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("fault", ["duplicate", "missing", "nonstring"])
def test_manifest_artifact_index_rejects_invalid_membership(v2_delivery_context, fault):
    from copy import deepcopy

    from apps.publications.document_v2 import DocumentV2Error, validate_ready_document_generation
    from apps.publications.models import DocumentGenerationManifest
    from apps.publications.restore import publication_restore_blocked_reason, restore_candidates

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    for name in ("A", "B"):
        _plan(user, department, accepted, name, b"PDF " + name.encode())
    publication = _generation(department, scope, 1)
    build_document_v2_generation(publication=publication)
    manifest = build_document_v2_manifest(publication=publication)
    payload = deepcopy(manifest.payload)
    if fault == "duplicate":
        payload["documents"][1]["artifact_id"] = payload["documents"][0]["artifact_id"]
    elif fault == "missing":
        payload["documents"][1]["artifact_id"] = str(uuid.uuid4())
    else:
        payload["documents"][1]["artifact_id"] = []
    manifest.delete()  # Only replace a BUILDING fixture, never a signed ready generation.
    DocumentGenerationManifest.objects.create(
        publication=publication,
        payload=payload,
        signature=b"s" * 64,
        signature_algorithm="Ed25519",
        signing_key_version="test",
    )
    with pytest.raises(DocumentV2Error):
        validate_ready_document_generation(publication, historical=True)
    candidate = restore_candidates(DatasetPublication.objects.filter(pk=publication.pk)).get()
    candidate.status, candidate.artifact_status, candidate.artifact_ready = (
        "SUPERSEDED",
        "READY",
        True,
    )
    assert (
        publication_restore_blocked_reason(candidate, scope=scope)
        == "Incomplete document generation"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("document_count", [2, 12])
def test_document_membership_index_reads_each_artifact_identity_once(
    v2_delivery_context, monkeypatch, document_count
):
    from types import SimpleNamespace

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.publications.document_v2 import validate_ready_document_generation
    from apps.publications.models import DocumentGenerationManifest
    from apps.publications.restore import publication_restore_blocked_reason, restore_candidates

    user, department, scope, accepted, _, _, _ = v2_delivery_context
    for number in range(document_count):
        _plan(user, department, accepted, str(number), f"PDF {number}".encode())
    publication = _generation(department, scope, 1)
    build_document_v2_generation(publication=publication)
    manifest = build_document_v2_manifest(publication=publication)

    class CountedDocument(dict):
        identity_reads = 0

        def get(self, key, default=None):
            if key == "artifact_id":
                type(self).identity_reads += 1
            return super().get(key, default)

    manifest.payload["documents"] = [CountedDocument(d) for d in manifest.payload["documents"]]
    # Keep real frozen adapter/key/reference validation; instrument only the
    # decoded manifest returned from storage, without rewriting it.
    with CaptureQueriesContext(connection) as queries:
        candidate = restore_candidates(DatasetPublication.objects.filter(pk=publication.pk)).get()
    assert len(queries) == 2
    monkeypatch.setattr(
        DocumentGenerationManifest.objects,
        "filter",
        lambda **kwargs: SimpleNamespace(first=lambda: manifest),
    )
    validate_ready_document_generation(publication, historical=True)
    assert CountedDocument.identity_reads == document_count
    CountedDocument.identity_reads = 0
    candidate.document_generation_manifest = manifest
    candidate.status, candidate.artifact_status, candidate.artifact_ready = (
        "SUPERSEDED",
        "READY",
        True,
    )
    with CaptureQueriesContext(connection) as queries:
        assert publication_restore_blocked_reason(candidate, scope=scope) == ""
    assert len(queries) == 0
    assert CountedDocument.identity_reads == document_count
