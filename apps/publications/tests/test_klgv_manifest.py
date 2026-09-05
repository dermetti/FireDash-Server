# ruff: noqa: E501
import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.builders import build_source_payload
from apps.publications.document_artifacts import release_terminal_document_artifact_references
from apps.publications.document_v2 import (
    build_document_v2_generation,
    build_document_v2_manifest,
    generation_hpke_context,
    process_next_generation_key_grant,
)
from apps.publications.feature_services import is_feature_enabled, set_department_feature
from apps.publications.features import FEATURE_REGISTRY
from apps.publications.hpke import (
    DocumentGenerationHPKEContext,
    HPKEError,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.manifests import manifest_publications, request_manifest
from apps.publications.models import (
    DepartmentFeature,
    DatasetPublication,
    DatasetScopeState,
    DocumentArtifact,
    DocumentGenerationKeyGrant,
    PublicationDocumentArtifactReference,
)
from apps.publications.registry import get_dataset_definition
from apps.publications.services import (
    enqueue_publication_job,
    mark_dirty,
    process_next_job,
    rollback_publication,
)
from apps.publications.worker_grants import process_next_signed_manifest
from apps.reference_data.models import KlgvPlan
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import generate_credential


@pytest.mark.django_db
def test_klgv_snapshot_uses_canonical_metadata(settings, tmp_path):
    settings.REFERENCE_DATA_ACCEPTED_ROOT = tmp_path
    actor = User.objects.create_user("klgv-manifest@example.test", "KLGV", "safe-password")
    department = Department.objects.create(name="KLGV", short_code="KLG", created_by=actor)
    document = b"%PDF-1.4\n%%EOF\n"
    digest = hashlib.sha256(document).hexdigest()
    plan = KlgvPlan.objects.create(
        department=department,
        external_identifier="K-1",
        object_name="Garden plan",
        address="Garden Way 1",
        postal_code="22041",
        city="Hamburg",
        location=Point(10.000992, 53.551323, srid=4326),
        path="plans/11111111-1111-1111-1111-111111111111.pdf",
        original_filename="uploaded.pdf",
        file_size=len(document),
        page_count=1,
        source_pdf_sha256=digest,
        sha256=digest,
        uploaded_by=actor,
    )
    accepted = tmp_path / plan.path
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(document)

    snapshot = build_source_payload(
        definition=get_dataset_definition("department_klgv_plans"),
        department=department,
        station=None,
    )
    item = snapshot["klgv_plans"][0]
    assert item == {
            "id": str(plan.id),
            "external_identifier": "K-1",
            "object_name": "Garden plan",
            "address": "Garden Way 1",
            "postal_code": "22041",
            "city": "Hamburg",
            "longitude": 10.000992,
            "latitude": 53.551323,
            "sha256": digest,
            "page_count": 1,
    }


@pytest.mark.django_db(transaction=True)
def test_klgv_v2_generation_is_complete_and_reuses_its_immutable_pdf(settings, tmp_path):
    settings.REFERENCE_DATA_ACCEPTED_ROOT = tmp_path / "accepted"
    actor = User.objects.create_user("klgv-v2@example.test", "KLGV", "safe-password")
    department = Department.objects.create(name="KLGV v2", short_code="K2", created_by=actor)
    pdf = b"%PDF-1.4\nKLGV\n%%EOF\n"
    digest = hashlib.sha256(pdf).hexdigest()
    plan = KlgvPlan.objects.create(
        department=department, external_identifier="K-2", object_name="KLGV",
        address="Garden Way 2", postal_code="22041", city="Hamburg", path="plans/k2.pdf",
        original_filename="k2.pdf", file_size=len(pdf), page_count=1,
        source_pdf_sha256=digest, sha256=digest, uploaded_by=actor,
    )
    accepted = settings.REFERENCE_DATA_ACCEPTED_ROOT / plan.path
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(pdf)
    scope = DatasetScopeState.objects.create(
        department=department, station=None, dataset_type_code="department_klgv_plans"
    )
    snapshot = build_source_payload(
        definition=get_dataset_definition("department_klgv_plans"), department=department, station=None
    )
    (tmp_path / "kek").write_bytes(base64.b64encode(b"a" * 32))
    (tmp_path / "signing").write_bytes(b"b" * 32)
    (tmp_path / "ring.json").write_text(
        '{"keys":{"1":"'
        + base64.b64encode(
            Ed25519PrivateKey.from_private_bytes(b"b" * 32).public_key().public_bytes_raw()
        ).decode("ascii")
        + '"}}',
        encoding="ascii",
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "tmp",
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=tmp_path / "ring.json",
    ):
        first = DatasetPublication.objects.create(
            department=department, scope_state=scope, dataset_type_code="department_klgv_plans",
            version_number=1, schema_version=2, source_revision=1, source_snapshot=snapshot,
            status=DatasetPublication.Status.BUILDING,
        )
        build_document_v2_generation(publication=first)
        manifest = build_document_v2_manifest(publication=first)
        second = DatasetPublication.objects.create(
            department=department, scope_state=scope, dataset_type_code="department_klgv_plans",
            version_number=2, schema_version=2, source_revision=2, source_snapshot=snapshot,
            status=DatasetPublication.Status.BUILDING,
        )
        build_document_v2_generation(publication=second)
    assert not first.artifact_path
    assert PublicationDocumentArtifactReference.objects.filter(publication=first).count() == 1
    assert DocumentArtifact.objects.count() == 1
    assert manifest.payload["dataset_type"] == "department_klgv_plans"
    assert set(manifest.payload["documents"][0]["klgv_plan"]) == {
        "id",
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "sha256",
        "page_count",
    }
    assert manifest.payload["documents"][0]["klgv_plan"]["id"] == str(plan.id)


@pytest.fixture
def klgv_delivery_context(db, tmp_path, monkeypatch):
    from apps.publications import artifacts

    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda _path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda _path: None)
    user = User.objects.create_user("klgv-delivery@example.test", "KLGV Delivery", "safe-password")
    department = Department.objects.create(name="KLGV delivery", short_code="KDV", created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="KST")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    tablet = Tablet.objects.create(department=department, display_name="Tablet", status=Tablet.Status.ACTIVE)
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential = generate_credential()
    installation = AppInstallation.objects.create(
        tablet=tablet, installation_uuid=tablet.id,
        credential_hash=hmac.new(settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256).hexdigest(),
        status=AppInstallation.Status.ACTIVE, app_version="1.0.0", adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(), hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite="DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        hpke_key_fingerprint="a" * 64, hpke_key_verified_at=timezone.now(), adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=user)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    kek, signing, ring = tmp_path / "kek", tmp_path / "signing", tmp_path / "ring.json"
    kek.write_bytes(b"k" * 32)
    signing.write_bytes(b"s" * 32)
    ring.write_text(json.dumps({"keys": {"1": base64.b64encode(Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().public_bytes_raw()).decode("ascii")}}), encoding="ascii")
    with override_settings(
        REFERENCE_DATA_ACCEPTED_ROOT=accepted, PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp", PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=kek, PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing,
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring,
    ):
        yield user, department, accepted, installation, private_key, credential


def _klgv_plan(*, user, department, accepted, identifier, pdf, name=None):
    digest = hashlib.sha256(pdf).hexdigest()
    plan = KlgvPlan.objects.create(
        department=department, external_identifier=identifier, object_name=name or identifier,
        address="Garden Way", postal_code="22041", city="Hamburg", path=f"plans/{uuid.uuid4()}.pdf",
        original_filename=f"{identifier}.pdf", file_size=len(pdf), page_count=1,
        source_pdf_sha256=digest, sha256=digest, uploaded_by=user,
    )
    path = accepted / plan.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)
    return plan


def _klgv_publication(*, department, scope, version, status=DatasetPublication.Status.BUILDING):
    snapshot = build_source_payload(
        definition=get_dataset_definition("department_klgv_plans"), department=department, station=None
    )
    return DatasetPublication.objects.create(
        department=department, scope_state=scope, dataset_type_code="department_klgv_plans",
        version_number=version, schema_version=2, source_revision=version,
        source_snapshot=snapshot, status=status,
    )


def _ready_artifact_publication(*, department, scope, dataset_type_code, version):
    """Create a v1 publication suitable for manifest-scope selection tests."""
    publication_id = uuid.uuid4()
    return DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        station=scope.station,
        scope_state=scope,
        dataset_type_code=dataset_type_code,
        version_number=version,
        schema_version=1,
        source_revision=version,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_ready=True,
        artifact_path=f"{department.id}/{publication_id}/artifact.bin",
        artifact_size=1,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"k" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="1",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="1",
    )


@pytest.mark.django_db(transaction=True)
def test_station_assigned_manifest_uses_default_feature_delivery_and_keeps_station_scope(
    klgv_delivery_context,
):
    """No feature row is needed for eligible data in an assigned tablet scope."""
    user, department, _, installation, _, _ = klgv_delivery_context
    vehicle = TabletVehicleAssignment.objects.get(tablet=installation.tablet).vehicle

    fire_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    fire = _ready_artifact_publication(
        department=department,
        scope=fire_scope,
        dataset_type_code="department_fire_plans",
        version=1,
    )
    fire_scope.current_published_publication = fire
    fire_scope.save(update_fields=("current_published_publication",))

    personnel_scope = DatasetScopeState.objects.create(
        department=department, station=vehicle.station, dataset_type_code="station_personnel"
    )
    personnel = _ready_artifact_publication(
        department=department,
        scope=personnel_scope,
        dataset_type_code="station_personnel",
        version=1,
    )
    personnel_scope.current_published_publication = personnel
    personnel_scope.save(update_fields=("current_published_publication",))

    _, resolved_vehicle, publications = manifest_publications(installation=installation)
    assert resolved_vehicle.id == vehicle.id
    assert {publication.id for publication in publications} == {fire.id, personnel.id}
    assert not DepartmentFeature.objects.filter(
        department=department, feature_code="klgv_plans"
    ).exists()
    assert all(
        is_feature_enabled(department=department, feature_code=feature_code)
        for feature_code in FEATURE_REGISTRY
    )

    klgv_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_klgv_plans"
    )
    klgv = _klgv_publication(department=department, scope=klgv_scope, version=1)
    klgv.status = klgv.Status.PUBLISHED
    klgv.artifact_status = klgv.ArtifactStatus.READY
    klgv.artifact_ready = True
    klgv.save(update_fields=("status", "artifact_status", "artifact_ready"))
    klgv_scope.current_published_publication = klgv
    klgv_scope.save(update_fields=("current_published_publication",))

    _, _, publications = manifest_publications(installation=installation)
    assert {publication.id for publication in publications} == {fire.id, klgv.id, personnel.id}
    assert klgv.station_id is None  # KLGV has no direct station assignment.

    other_department = Department.objects.create(
        name="Other department", short_code="OTH", created_by=user
    )
    other_station = Station.objects.create(
        department=other_department, name="Other station", short_code="OST"
    )
    other_vehicle = Vehicle.objects.create(
        department=other_department, station=other_station, display_name="Other engine"
    )
    other_tablet = Tablet.objects.create(
        department=other_department, display_name="Other tablet", status=Tablet.Status.ACTIVE
    )
    other_installation = AppInstallation.objects.create(
        tablet=other_tablet,
        installation_uuid=other_tablet.id,
        credential_hash="x" * 64,
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=installation.hpke_public_key,
        hpke_ciphersuite=installation.hpke_ciphersuite,
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(
        tablet=other_tablet, vehicle=other_vehicle, valid_from=timezone.now(), created_by=user
    )
    _, _, other_publications = manifest_publications(installation=other_installation)
    assert klgv.id not in {publication.id for publication in other_publications}

    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=False)
    _, _, publications = manifest_publications(installation=installation)
    assert {publication.id for publication in publications} == {fire.id, personnel.id}

    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=True)
    klgv.status = klgv.Status.SUPERSEDED
    klgv.save(update_fields=("status",))
    klgv_scope.current_published_publication = None
    klgv_scope.save(update_fields=("current_published_publication",))
    _, _, publications = manifest_publications(installation=installation)
    assert {publication.id for publication in publications} == {fire.id, personnel.id}


@pytest.mark.django_db(transaction=True)
def test_klgv_v2_delivery_is_discoverable_and_client_decryptable(klgv_delivery_context):
    user, department, accepted, installation, private_key, credential = klgv_delivery_context
    _klgv_plan(user=user, department=department, accepted=accepted, identifier="A", pdf=b"PDF A")
    scope = DatasetScopeState.objects.create(department=department, dataset_type_code="department_klgv_plans")
    publication = _klgv_publication(department=department, scope=scope, version=1)
    references = build_document_v2_generation(publication=publication)
    manifest = build_document_v2_manifest(publication=publication)
    publication.status, publication.artifact_status, publication.artifact_ready = (
        publication.Status.PUBLISHED, publication.ArtifactStatus.READY, True
    )
    publication.save(update_fields=("status", "artifact_status", "artifact_ready"))
    scope.current_published_publication = publication
    scope.save(update_fields=("current_published_publication",))
    failed_replacement = _klgv_publication(department=department, scope=scope, version=2)
    failed_replacement.status = failed_replacement.Status.FAILED
    failed_replacement.save(update_fields=("status",))

    client = Client()
    auth = {"HTTP_AUTHORIZATION": f"Bearer {credential}"}
    # No ready grant means no ciphertext, even for a valid same-scope installation.
    document = manifest.payload["documents"][0]
    download = f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{document['artifact_id']}/download"
    assert client.get(download, **auth).status_code == 403
    assert client.get(f"/api/v1/tablet/document-generations/{publication.id}/manifest", **auth).status_code == 202
    grant = process_next_generation_key_grant()
    assert grant is not None and grant.status == DocumentGenerationKeyGrant.Status.READY
    response = client.get(f"/api/v1/tablet/document-generations/{publication.id}/manifest", **auth)
    assert response.status_code == 200
    wire = response.json()["generation_key_grant"]["info"]
    context = DocumentGenerationHPKEContext.from_wire(wire)
    assert context.info() == generation_hpke_context(publication=publication, installation=installation).info()
    generation_key = hpke_open(
        encapsulated_key=base64.b64decode(response.json()["generation_key_grant"]["encapsulated_key"]),
        ciphertext=base64.b64decode(response.json()["generation_key_grant"]["wrapped_generation_key"]),
        recipient_private_key=private_key, context=context,
    )
    with pytest.raises(HPKEError):
        hpke_open(encapsulated_key=base64.b64decode(response.json()["generation_key_grant"]["encapsulated_key"]), ciphertext=base64.b64decode(response.json()["generation_key_grant"]["wrapped_generation_key"]), recipient_private_key=private_key, context=DocumentGenerationHPKEContext(**{**context.__dict__, "installation_id": uuid.uuid4()}))
    cek = keywrap.aes_key_unwrap(generation_key, base64.b64decode(document["generation_wrapped_cek"]))
    reference = references[0]
    ciphertext = (settings.PUBLICATION_ARTIFACT_ROOT / reference.document_artifact.artifact_path).read_bytes()
    assert len(ciphertext) == document["ciphertext_size"]
    assert hashlib.sha256(ciphertext).hexdigest() == document["ciphertext_sha256"]
    plaintext = AESGCM(cek).decrypt(base64.b64decode(document["nonce"]), ciphertext, None)
    assert hashlib.sha256(plaintext).hexdigest() == document["sanitized_pdf_sha256"]
    assert client.get(download, **auth).status_code == 200
    assert client.get(f"/api/v1/tablet/document-generations/{publication.id}/artifacts/{uuid.uuid4()}/download", **auth).status_code == 404
    with pytest.raises(InvalidTag):
        AESGCM(cek).decrypt(base64.b64decode(document["nonce"]), ciphertext[:-1] + b"x", None)

    # A previously-ready grant is not a capability that outlives feature
    # disablement; both generation and individual artifact endpoints stay
    # behind the current delivery authorization decision.
    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=False)
    assert client.get(f"/api/v1/tablet/document-generations/{publication.id}/manifest", **auth).status_code == 403
    assert client.get(download, **auth).status_code == 403
    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=True)

    request_manifest(installation=installation)
    assert process_next_signed_manifest() is not None
    discovered = client.get("/api/v1/tablet/manifest", **auth)
    assert discovered.status_code == 200
    assert any(entry == {
        "publication_id": str(publication.id), "type": "department_klgv_plans", "scope": "department",
        "version": 1, "schema_version": 2, "required": True, "minimum_app_version": None,
        "artifact_format": "document-manifest-v2",
        "manifest_url": f"/api/v1/tablet/document-generations/{publication.id}/manifest",
    } for entry in discovered.json()["datasets"])
    assert all(
        entry["publication_id"] != str(failed_replacement.id)
        for entry in discovered.json()["datasets"]
    )
    scope.refresh_from_db()
    assert scope.current_published_publication_id == publication.id
    outsider_department = Department.objects.create(
        name="Other KLGV", short_code="OKD", created_by=user
    )
    outsider_tablet = Tablet.objects.create(
        department=outsider_department, display_name="Other tablet", status=Tablet.Status.ACTIVE
    )
    outsider_station = Station.objects.create(
        department=outsider_department, name="Other", short_code="OST"
    )
    outsider_vehicle = Vehicle.objects.create(
        department=outsider_department, station=outsider_station, display_name="Other engine"
    )
    outsider_credential = generate_credential()
    outsider_private_key = ec.generate_private_key(ec.SECP256R1())
    AppInstallation.objects.create(
        tablet=outsider_tablet,
        installation_uuid=outsider_tablet.id,
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), outsider_credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=serialize_p256_public_key(outsider_private_key.public_key()),
        hpke_ciphersuite="DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(
        tablet=outsider_tablet, vehicle=outsider_vehicle, valid_from=timezone.now(), created_by=user
    )
    assert client.get(download, HTTP_AUTHORIZATION=f"Bearer {outsider_credential}").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_klgv_normal_lifecycle_rollback_and_feature_gate(klgv_delivery_context):
    user, department, accepted, installation, _, credential = klgv_delivery_context
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    # Preparation is deliberately independent from rollout exposure. KLGV is
    # required when enabled, but starts disabled for this department.
    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=False)
    _klgv_plan(user=user, department=department, accepted=accepted, identifier="A", pdf=b"PDF A")
    mark_dirty(
        actor=user, department=department, dataset_type_code="department_klgv_plans"
    )
    enqueue_publication_job(
        department=department,
        dataset_type_code="department_klgv_plans",
        requested_by=user,
        trigger_type="USER_REQUEST",
        allow_clean_rebuild=True,
    )
    first_job = process_next_job()
    assert first_job is not None and first_job.status == first_job.Status.SUCCEEDED
    first = DatasetPublication.objects.get(pk=first_job.build_publication_id)
    assert first.status == first.Status.PUBLISHED
    assert first.schema_version == 2

    client = Client()
    auth = {"HTTP_AUTHORIZATION": f"Bearer {credential}"}
    request_manifest(installation=installation)
    assert process_next_signed_manifest() is not None
    assert all(
        entry["type"] != "department_klgv_plans"
        for entry in client.get("/api/v1/tablet/manifest", **auth).json()["datasets"]
    )
    # A CURRENT publication is not a delivery authorization while its feature
    # is disabled, including the document-generation grant endpoint.
    assert client.get(
        f"/api/v1/tablet/document-generations/{first.id}/manifest", **auth
    ).status_code == 403

    publication_count = DatasetPublication.objects.filter(
        department=department, dataset_type_code="department_klgv_plans"
    ).count()
    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=True)
    request_manifest(installation=installation)
    assert process_next_signed_manifest() is not None
    assert DatasetPublication.objects.filter(
        department=department, dataset_type_code="department_klgv_plans"
    ).count() == publication_count
    first_entry = next(
        entry
        for entry in client.get("/api/v1/tablet/manifest", **auth).json()["datasets"]
        if entry["type"] == "department_klgv_plans"
    )
    assert first_entry == {
        "publication_id": str(first.id),
        "type": "department_klgv_plans",
        "scope": "department",
        "version": 1,
        "schema_version": 2,
        "required": True,
        "minimum_app_version": None,
        "artifact_format": "document-manifest-v2",
        "manifest_url": f"/api/v1/tablet/document-generations/{first.id}/manifest",
    }

    _klgv_plan(user=user, department=department, accepted=accepted, identifier="B", pdf=b"PDF B")
    mark_dirty(
        actor=user, department=department, dataset_type_code="department_klgv_plans"
    )
    enqueue_publication_job(
        department=department,
        dataset_type_code="department_klgv_plans",
        requested_by=user,
        trigger_type="USER_REQUEST",
        allow_clean_rebuild=True,
    )
    second_job = process_next_job()
    assert second_job is not None and second_job.status == second_job.Status.SUCCEEDED
    second = DatasetPublication.objects.get(pk=second_job.build_publication_id)
    first.refresh_from_db()
    assert (first.status, second.status) == (first.Status.SUPERSEDED, second.Status.PUBLISHED)

    rollback_publication(actor=user, publication=first)
    request_manifest(installation=installation)
    restored = next(
        entry
        for entry in client.get("/api/v1/tablet/manifest", **auth).json()["datasets"]
        if entry["type"] == "department_klgv_plans"
    )
    assert restored["publication_id"] == str(first.id)

    set_department_feature(actor=user, department=department, feature_code="klgv_plans", enabled=False)
    request_manifest(installation=installation)
    # This may reuse the previously signed empty state; no build or delivery
    # grant is needed to hide a disabled dataset.
    process_next_signed_manifest()
    assert all(
        entry["type"] != "department_klgv_plans"
        for entry in client.get("/api/v1/tablet/manifest", **auth).json()["datasets"]
    )


@pytest.mark.django_db(transaction=True)
def test_klgv_change_matrix_and_retained_references_protect_artifacts(klgv_delivery_context):
    user, department, accepted, _, _, _ = klgv_delivery_context
    first = _klgv_plan(user=user, department=department, accepted=accepted, identifier="A", pdf=b"PDF A")
    stable = _klgv_plan(user=user, department=department, accepted=accepted, identifier="B", pdf=b"PDF B")
    scope = DatasetScopeState.objects.create(department=department, dataset_type_code="department_klgv_plans")
    v1 = _klgv_publication(department=department, scope=scope, version=1)
    build_document_v2_generation(publication=v1)
    v1_artifacts = dict(PublicationDocumentArtifactReference.objects.filter(publication=v1).values_list("canonical_document_id", "document_artifact_id"))
    # Metadata-only snapshot change keeps both immutable artifacts.
    first.object_name = "Renamed only"
    first.save(update_fields=("object_name",))
    v2 = _klgv_publication(department=department, scope=scope, version=2)
    build_document_v2_generation(publication=v2)
    assert dict(PublicationDocumentArtifactReference.objects.filter(publication=v2).values_list("canonical_document_id", "document_artifact_id")) == v1_artifacts
    # One changed PDF replaces exactly its artifact; an addition creates exactly one more.
    changed = b"PDF A changed"
    first.sha256 = first.source_pdf_sha256 = hashlib.sha256(changed).hexdigest()
    first.file_size = len(changed)
    first.save(update_fields=("sha256", "source_pdf_sha256", "file_size"))
    (accepted / first.path).write_bytes(changed)
    added = _klgv_plan(user=user, department=department, accepted=accepted, identifier="C", pdf=b"PDF C")
    v3 = _klgv_publication(department=department, scope=scope, version=3)
    build_document_v2_generation(publication=v3)
    v3_artifacts = dict(PublicationDocumentArtifactReference.objects.filter(publication=v3).values_list("canonical_document_id", "document_artifact_id"))
    assert v3_artifacts[stable.id] == v1_artifacts[stable.id]
    assert v3_artifacts[first.id] != v1_artifacts[first.id]
    assert added.id in v3_artifacts and DocumentArtifact.objects.count() == 4
    # A removed document is absent from the next complete generation; its old artifact remains
    # while v1/v2 are retained, then becomes eligible only after their references are released.
    removed_plan_id = first.id
    first.delete()
    v4 = _klgv_publication(department=department, scope=scope, version=4)
    build_document_v2_generation(publication=v4)
    assert set(PublicationDocumentArtifactReference.objects.filter(publication=v4).values_list("canonical_document_id", flat=True)) == {stable.id, added.id}
    # The replacement is referenced only by v3 after the plan is removed from v4.
    # It must survive until that retained generation is released.
    old_artifact = v3_artifacts[removed_plan_id]
    old_path = settings.PUBLICATION_ARTIFACT_ROOT / DocumentArtifact.objects.get(
        pk=old_artifact
    ).artifact_path
    for publication in (v1, v2):
        publication.status = publication.Status.OBSOLETE
        publication.save(update_fields=("status",))
        with transaction.atomic():
            release_terminal_document_artifact_references(publication=publication)
    assert DocumentArtifact.objects.filter(pk=old_artifact).exists()
    v3.status = v3.Status.OBSOLETE
    v3.save(update_fields=("status",))
    with transaction.atomic():
        assert release_terminal_document_artifact_references(publication=v3) >= 1
    assert not DocumentArtifact.objects.filter(pk=old_artifact).exists()
    assert not old_path.exists()
