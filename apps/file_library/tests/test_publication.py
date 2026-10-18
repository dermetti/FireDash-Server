# ruff: noqa: F811
import base64
import hashlib
import hmac
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, transaction
from django.urls import reverse
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.file_library import services
from apps.file_library.models import Content, PublicationContent
from apps.file_library.publication import initialize_missing_libraries, publish_now, scope_kwargs
from apps.file_library.reconciliation import reconcile_orphans
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.organizations.models import Vehicle
from apps.publications.document_v2 import process_next_generation_key_grant
from apps.publications.hpke import (
    DocumentGenerationHPKEContext,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.manifests import canonical_manifest_payload, manifest_publications
from apps.publications.models import (
    DatasetScopeState,
    DocumentArtifact,
    DocumentGenerationManifest,
    PublicationJob,
)
from apps.publications.retention import run_publication_retention
from apps.publications.services import (
    build_claimed_job,
    claim_next_job,
    process_next_job,
    request_rebuild,
)
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import generate_credential

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def crypto(settings, tmp_path):
    (tmp_path / "kek").write_bytes(base64.b64encode(b"a" * 32))
    (tmp_path / "signing").write_bytes(b"b" * 32)
    key = Ed25519PrivateKey.from_private_bytes(b"b" * 32).public_key()
    (tmp_path / "ring.json").write_text(
        json.dumps({"keys": {"1": base64.b64encode(key.public_bytes_raw()).decode()}})
    )
    settings.PUBLICATION_ARTIFACT_ROOT = tmp_path / "artifacts"
    settings.PUBLICATION_ARTIFACT_TEMP_ROOT = tmp_path / "artifact-temp"
    settings.PUBLICATION_KEK_CREDENTIAL_PATH = tmp_path / "kek"
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH = tmp_path / "signing"
    settings.PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH = tmp_path / "ring.json"
    settings.PUBLICATION_SIGNING_KEY_VERSION = "1"
    settings.PUBLICATION_KEK_VERSION = "1"
    return key


def publish(actor, library):
    publish_now(actor=actor, library_id=library.id)
    job = process_next_job()
    assert job.status == "SUCCEEDED", (job.error_message, job.status)
    return job.build_publication


def test_empty_scheduled_and_manual_libraries(scope, crypto):
    initialize_missing_libraries()
    assert (
        DatasetScopeState.objects.filter(
            dataset_type_code__in=("department_files", "station_files")
        ).count()
        == 4
    )
    assert process_next_job() is None
    library = library_for(scope)
    generation = publish(scope[0], library)
    manifest = DocumentGenerationManifest.objects.get(publication=generation)
    assert manifest.payload["documents"] == manifest.payload["folders"] == []
    assert manifest.payload["source_revision"] == 0
    assert generation.artifact_path == "" and not DocumentArtifact.objects.exists()
    crypto.verify(bytes(manifest.signature), canonical_manifest_payload(manifest.payload))
    PublicationJob.objects.filter(status="PENDING").update(not_before=timezone.now())
    assert process_next_job().status == "SUCCEEDED"


def test_frozen_metadata_reuse_replacement_and_retention(scope, pdf_pipeline, crypto, settings):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    folder = services.create_folder(**common, name="Guides")
    doc = services.upload_pdf(**common, upload=pdf(), parent_id=folder.id)
    original = doc.content
    first = publish(scope[0], library)
    manifest = DocumentGenerationManifest.objects.get(publication=first)
    assert manifest.payload["folders"] == [
        {"id": str(folder.id), "name": "Guides", "parent_id": None}
    ]
    assert manifest.payload["documents"][0]["file"]["id"] == str(doc.id)
    crypto.verify(bytes(manifest.signature), canonical_manifest_payload(manifest.payload))
    artifact = DocumentArtifact.objects.get()
    services.edit_entry(**common, entry_id=doc.id, name="Renamed.pdf")
    second = publish(scope[0], library)
    assert DocumentArtifact.objects.count() == 1
    assert second.document_artifact_references.get().document_artifact_id == artifact.id
    services.upload_pdf(**common, upload=pdf(pages=2), replace_id=doc.id)
    third = publish(scope[0], library)
    assert not services.collect_content(original.id)
    services.delete_entry(**common, entry_id=folder.id)
    fourth = publish(scope[0], library)
    assert DocumentGenerationManifest.objects.get(publication=fourth).payload["documents"] == []
    result = run_publication_retention()
    assert result["obsoleted"] == 1
    first.refresh_from_db()
    assert first.status == "OBSOLETE"
    assert not PublicationContent.objects.filter(publication=first).exists()
    assert not services.collect_content(original.id)  # second still protects it
    services.rename_library(**common, name="New name")
    publish(scope[0], library)
    run_publication_retention()
    assert services.collect_content(original.id)
    assert not services.content_path(original).exists()
    library.refresh_from_db()
    assert library.usage_bytes == 0
    assert third.document_artifact_references.exists()


def test_revision_race_and_failure_preserve_previous(scope, pdf_pipeline, crypto, monkeypatch):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    doc = services.upload_pdf(**common, upload=pdf())
    first = publish(scope[0], library)
    services.rename_library(**common, name="Second")
    publish_now(**common)
    job = claim_next_job()
    old = doc.content
    services.upload_pdf(**common, upload=pdf(pages=2), replace_id=doc.id)
    assert not services.collect_content(old.id)
    completed = build_claimed_job(job_id=job.id)
    assert completed.status == "OBSOLETE"
    assert (
        DatasetScopeState.objects.get(**scope_kwargs(library)).current_published_publication_id
        == first.id
    )
    from apps.publications import document_v2
    from apps.publications.builders import PublicationBuildError

    def fail(**kwargs):
        raise PublicationBuildError("test failure")

    monkeypatch.setattr(document_v2, "build_document_v2_generation", fail)
    assert process_next_job().status == "FAILED"
    assert (
        DatasetScopeState.objects.get(**scope_kwargs(library)).current_published_publication_id
        == first.id
    )


def test_publication_permissions_and_ui(scope, client, crypto):
    library = library_for(scope)
    station_library = services.get_library(scope[1], station_id=scope[5].id)
    for actor, target in ((scope[0], station_library), (scope[1], library), (scope[2], library)):
        with pytest.raises(PermissionDenied):
            publish_now(actor=actor, library_id=target.id)
    with pytest.raises(PermissionDenied):
        request_rebuild(
            actor=scope[0], department=scope[3], station=scope[5], dataset_type_code="station_files"
        )
    with pytest.raises(PermissionDenied):
        request_rebuild(
            actor=scope[1],
            department=scope[3],
            station=scope[5],
            dataset_type_code="station_personnel",
        )
    client.force_login(scope[1])
    url = f"/files/{station_library.id}/"
    assert client.post(url, {"action": "publish"}).status_code == 302
    assert b"Publish Now" in client.get(url).content
    generation = process_next_job().build_publication
    assert generation.station_id == scope[5].id
    client.force_login(scope[0])
    assert (
        client.get(
            reverse("publications-scope-detail", args=[generation.scope_state_id])
        ).status_code
        == 403
    )
    response = client.get(reverse("publications-list", args=[scope[3].id]))
    assert b"Station files" not in response.content


def test_orphan_reconciliation_protects_committed_and_recent_files(scope, pdf_pipeline):
    library = library_for(scope)
    doc = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    path = services.content_path(doc.content)
    old = (timezone.now() - timedelta(days=10)).timestamp()
    os.utime(path, (old, old))
    orphan = path.parent / f"{uuid.uuid4()}.pdf"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (old, old))
    recent = path.parent / f"{uuid.uuid4()}.pdf"
    recent.write_bytes(b"recent")
    rolled_back = path.parent.parent / str(uuid.uuid4()) / f"{uuid.uuid4()}.pdf"
    rolled_back.parent.mkdir()
    rolled_back.write_bytes(b"rolled back library")
    os.utime(rolled_back, (old, old))
    assert reconcile_orphans(dry_run=True) == 2 and orphan.exists()
    assert reconcile_orphans() == 2
    assert reconcile_orphans() == 0 and path.exists() and recent.exists()


def installation_for(scope, settings):
    tablet = Tablet.objects.create(
        department=scope[3], display_name="Library tablet", status="ACTIVE"
    )
    vehicle = Vehicle.objects.create(department=scope[3], station=scope[5], display_name="Engine")
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=scope[0]
    )
    private = ec.generate_private_key(ec.SECP256R1())
    credential = generate_credential()
    install = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status="ACTIVE",
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=serialize_p256_public_key(private.public_key()),
        hpke_ciphersuite="DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    return install, private, credential, vehicle


def test_station_manifest_crypto_and_reassignment(scope, pdf_pipeline, crypto, settings, client):
    install, private, credential, vehicle = installation_for(scope, settings)
    department_library = library_for(scope)
    department_generation = publish(scope[0], department_library)
    library = services.get_library(scope[1], station_id=scope[5].id)
    services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    generation = publish(scope[1], library)
    _, _, discovered = manifest_publications(installation=install)
    assert {p.pk for p in discovered} == {generation.pk, department_generation.pk}
    url = f"/api/v1/tablet/document-generations/{generation.id}/manifest"
    auth = {"HTTP_AUTHORIZATION": f"Bearer {credential}"}
    from apps.publications.manifests import request_manifest
    from apps.publications.worker_grants import process_next_signed_manifest

    request_manifest(installation=install)
    process_next_signed_manifest()
    complete = client.get("/api/v1/tablet/manifest", **auth)
    assert complete.status_code == 200
    descriptors = {entry["type"]: entry for entry in complete.json()["datasets"]}
    assert set(descriptors) == {"department_files", "station_files"}
    assert all(
        entry["required"] and entry["artifact_format"] == "document-manifest-v2"
        for entry in descriptors.values()
    )
    assert descriptors["station_files"]["scope"]["station_id"] == str(scope[5].id)
    assert client.get(url, **auth).status_code == 202
    assert process_next_generation_key_grant().status == "READY"
    response = client.get(url, **auth)
    assert response.status_code == 200
    payload = response.json()
    grant = payload["generation_key_grant"]
    context = DocumentGenerationHPKEContext.from_wire(grant["info"])
    assert context.station_id == scope[5].id
    key = hpke_open(
        encapsulated_key=base64.b64decode(grant["encapsulated_key"]),
        ciphertext=base64.b64decode(grant["wrapped_generation_key"]),
        recipient_private_key=private,
        context=context,
    )
    document = payload["documents"][0]
    cek = keywrap.aes_key_unwrap(key, base64.b64decode(document["generation_wrapped_cek"]))
    artifact = DocumentArtifact.objects.get(pk=document["artifact_id"])
    ciphertext = (settings.PUBLICATION_ARTIFACT_ROOT / artifact.artifact_path).read_bytes()
    assert hashlib.sha256(ciphertext).hexdigest() == document["ciphertext_sha256"]
    plaintext = AESGCM(cek).decrypt(base64.b64decode(document["nonce"]), ciphertext, None)
    assert hashlib.sha256(plaintext).hexdigest() == document["sanitized_pdf_sha256"]
    assert client.get(document["download_path"], **auth).status_code == 200
    services.rename_library(actor=scope[1], library_id=library.id, name="Updated")
    publish(scope[1], library)
    assert client.get(url, **auth).status_code == 200  # retained history
    vehicle.station = scope[6]
    vehicle.save(update_fields=("station",))
    assert client.get(url, **auth).status_code == 403
    assert client.get(document["download_path"], **auth).status_code == 403
    install.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    install.save(update_fields=("authorization_valid_until",))
    assert client.get(url, **auth).status_code in {401, 403}


def test_database_generation_guards(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    doc = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    generation = publish(scope[0], library)
    artifact = generation.document_artifact_references.get().document_artifact
    for mutate in (
        lambda: PublicationContent.objects.filter(publication=generation).delete(),
        lambda: generation.document_artifact_references.all().delete(),
        lambda: DocumentArtifact.objects.filter(pk=artifact.pk).update(ciphertext_sha256="0" * 64),
        lambda: DocumentGenerationManifest.objects.filter(publication=generation).update(
            payload={}
        ),
        lambda: type(generation).objects.filter(pk=generation.pk).update(source_snapshot={}),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            mutate()
    assert Content.objects.filter(pk=doc.content_id).exists()


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_active_build_alone_protects_removed_content(scope, pdf_pipeline, crypto, operation):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    doc = services.upload_pdf(**common, upload=pdf())
    original = doc.content
    publish_now(**common)
    job = claim_next_job()
    if operation == "replace":
        services.upload_pdf(**common, upload=pdf(pages=2), replace_id=doc.id)
    else:
        services.delete_entry(**common, entry_id=doc.id)
    assert not services.collect_content(original.id)
    assert build_claimed_job(job_id=job.id).status == "OBSOLETE"
    assert services.collect_content(original.id)
    assert process_next_job().status == "SUCCEEDED"


def test_identical_pdf_bytes_do_not_reuse_cross_document_or_scope(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    data = pdf().read()
    docs = [
        services.upload_pdf(
            actor=scope[0], library_id=library.id, upload=SimpleUploadedFile(name, data)
        )
        for name in ("one.pdf", "two.pdf")
    ]
    generation = publish(scope[0], library)
    station_library = services.get_library(scope[1], station_id=scope[5].id)
    station_doc = services.upload_pdf(
        actor=scope[1], library_id=station_library.id, upload=SimpleUploadedFile("one.pdf", data)
    )
    station_generation = publish(scope[1], station_library)
    artifacts = list(DocumentArtifact.objects.all())
    assert len(artifacts) == 3
    assert len({artifact.sanitized_pdf_sha256 for artifact in artifacts}) == 1
    assert len({artifact.ciphertext_sha256 for artifact in artifacts}) == 3
    assert {ref.canonical_document_id for ref in generation.document_artifact_references.all()} == {
        doc.id for doc in docs
    }
    assert (
        station_generation.document_artifact_references.get().canonical_document_id
        == station_doc.id
    )


def test_reconciliation_serializes_with_uncommitted_promotion(scope, pdf_pipeline, monkeypatch):
    library = library_for(scope)
    promoted, cleanup_started, release = Event(), Event(), Event()
    original = services.storage.promote_to_accepted

    def promote(output, key):
        result = original(output, key)
        path = services.settings.REFERENCE_DATA_ACCEPTED_ROOT / key
        old = (timezone.now() - timedelta(days=10)).timestamp()
        os.utime(path, (old, old))
        promoted.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(services.storage, "promote_to_accepted", promote)

    def upload():
        close_old_connections()
        try:
            return services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf()).content
        finally:
            close_old_connections()

    def cleanup():
        close_old_connections()
        cleanup_started.set()
        try:
            return reconcile_orphans()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        uploading = pool.submit(upload)
        assert promoted.wait(10)
        collecting = pool.submit(cleanup)
        assert cleanup_started.wait(10)
        release.set()
        content = uploading.result(timeout=15)
        assert collecting.result(timeout=15) == 0
    assert services.content_path(content).exists()


def test_claim_serializes_with_source_mutation(scope, pdf_pipeline, crypto, monkeypatch):
    library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    publish_now(actor=scope[0], library_id=library.id)
    from apps.file_library import publication

    original = publication.source_payload
    snapshot_read, edit_started, release = Event(), Event(), Event()

    def read(**kwargs):
        snapshot = original(**kwargs)
        if not snapshot_read.is_set():
            snapshot_read.set()
            assert release.wait(10)
        return snapshot

    monkeypatch.setattr(publication, "source_payload", read)

    def claim():
        close_old_connections()
        try:
            return claim_next_job()
        finally:
            close_old_connections()

    def edit():
        close_old_connections()
        edit_started.set()
        try:
            services.rename_library(
                actor=scope[0], library_id=library.id, name="Changed concurrently"
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(claim)
        assert snapshot_read.wait(10)
        editing = pool.submit(edit)
        assert edit_started.wait(10)
        release.set()
        job = claiming.result(timeout=15)
        editing.result(timeout=15)
    assert job.build_publication.source_snapshot["library"]["name"] == "Documents"
    assert build_claimed_job(job_id=job.id).status == "OBSOLETE"
