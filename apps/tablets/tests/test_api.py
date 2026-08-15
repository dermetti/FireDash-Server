import base64
import hashlib
import hmac
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE, serialize_p256_public_key
from apps.publications.manifests import request_manifest
from apps.publications.models import DatasetKeyGrant, DatasetPublication, DatasetScopeState
from apps.publications.worker_grants import process_next_signed_manifest
from apps.tablets.api import DownloadView
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import generate_credential, verify_credential


@pytest.fixture
def api_context(db):
    now = timezone.now()
    user = User.objects.create_user("api@example.test", "API User", "safe-password")
    department = Department.objects.create(name="API", short_code="API", created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Engine 1"
    )
    tablet = Tablet.objects.create(department=department, display_name="Tablet")
    credential = generate_credential()
    key = ec.generate_private_key(ec.SECP256R1())
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        hpke_public_key=serialize_p256_public_key(key.public_key()),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_key_fingerprint="a" * 64,
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
    pub_id = uuid.uuid4()
    publication = DatasetPublication.objects.create(
        id=pub_id,
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=scope,
        version_number=1,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path=f"{department.id}/{pub_id}/artifact.bin",
        artifact_size=12,
        artifact_sha256="b" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"k" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="1",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="1",
    )
    DatasetKeyGrant.objects.create(
        publication=publication,
        app_installation=installation,
        status=DatasetKeyGrant.Status.READY,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_encapsulated_key=b"e",
        hpke_wrapped_content_key=b"w",
    )
    return Client(), installation, credential, publication


def _authorization(credential):
    return {"HTTP_AUTHORIZATION": f"Bearer {credential}"}


def test_configuration_and_manifest_use_bearer_installation_scope(api_context, tmp_path):
    client, installation, credential, publication = api_context
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    request_manifest(installation=installation)
    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        process_next_signed_manifest()

    configuration = client.get("/api/v1/tablet/configuration", **_authorization(credential))
    assert configuration.status_code == 200
    assert configuration.json()["installation_id"] == str(installation.id)

    manifest = client.get("/api/v1/tablet/manifest", **_authorization(credential))
    assert manifest.status_code == 200
    assert manifest.json()["datasets"][0]["publication_id"] == str(publication.id)
    assert manifest.json()["datasets"][0]["download_url"].endswith(f"/{publication.id}/download")
    assert manifest.json()["signature_algorithm"] == "Ed25519"
    assert manifest.json()["datasets"][0]["content_encryption_nonce"] == base64.b64encode(
        b"n" * 12
    ).decode("ascii")
    assert manifest["ETag"]

    signing_public_path = tmp_path / "signing-public"
    signing_public_path.write_bytes(
        Ed25519PrivateKey.from_private_bytes(b"s" * 32)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    with override_settings(PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH=signing_public_path):
        signing_key = client.get("/api/v1/tablet/signing-keys/1", **_authorization(credential))
    assert signing_key.status_code == 200
    assert signing_key.json() == {
        "algorithm": "Ed25519",
        "version": "1",
        "public_key": base64.b64encode(signing_public_path.read_bytes()).decode("ascii"),
    }
    unknown_key = client.get("/api/v1/tablet/signing-keys/unknown", **_authorization(credential))
    assert unknown_key.status_code == 404

    not_modified = client.get(
        "/api/v1/tablet/manifest",
        HTTP_IF_NONE_MATCH=manifest["ETag"],
        **_authorization(credential),
    )
    assert not_modified.status_code == 304


def test_manifest_pending_is_an_rfc9457_problem(api_context):
    client, _, credential, _ = api_context

    response = client.get("/api/v1/tablet/manifest", **_authorization(credential))

    assert response.status_code == 202
    assert response["Content-Type"].startswith("application/problem+json")
    assert response["Retry-After"] == "5"
    assert response.json()["type"].endswith("/manifest-pending")
    assert response.json()["manifest_request_id"]


def test_download_uses_canonical_artifact_path_and_etag(api_context, tmp_path):
    client, installation, credential, publication = api_context
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    request_manifest(installation=installation)
    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        process_next_signed_manifest()
    response = client.get(
        f"/api/v1/tablet/datasets/{publication.id}/download", **_authorization(credential)
    )

    assert response.status_code == 200
    assert response["X-Accel-Redirect"] == (
        f"/internal-protected-datasets/{publication.artifact_path}"
    )
    assert response["Accept-Ranges"] == "bytes"
    assert response["ETag"] == '"' + publication.artifact_sha256 + '"'

    not_modified = client.get(
        f"/api/v1/tablet/datasets/{publication.id}/download",
        HTTP_IF_NONE_MATCH=response["ETag"],
        **_authorization(credential),
    )
    assert not_modified.status_code == 304
    invalid_uuid = client.get(
        "/api/v1/tablet/datasets/not-a-uuid/download", **_authorization(credential)
    )
    assert invalid_uuid.status_code == 404


def test_download_accepts_octet_stream_content_negotiation(api_context, tmp_path):
    client, installation, credential, publication = api_context
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    request_manifest(installation=installation)
    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        process_next_signed_manifest()

    response = client.get(
        f"/api/v1/tablet/datasets/{publication.id}/download",
        HTTP_ACCEPT="application/octet-stream",
        **_authorization(credential),
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/octet-stream"
    assert response["X-Accel-Redirect"] == (
        f"/internal-protected-datasets/{publication.artifact_path}"
    )
    assert response["Accept-Ranges"] == "bytes"
    assert response["ETag"] == '"' + publication.artifact_sha256 + '"'

    not_modified = client.get(
        f"/api/v1/tablet/datasets/{publication.id}/download",
        HTTP_ACCEPT="application/octet-stream",
        HTTP_IF_NONE_MATCH=response["ETag"],
        **_authorization(credential),
    )
    assert not_modified.status_code == 304


def test_download_errors_stay_problem_json_with_octet_stream_accept(api_context, tmp_path):
    client, installation, credential, publication = api_context
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    request_manifest(installation=installation)
    with override_settings(PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path):
        process_next_signed_manifest()

    unauthorized = client.get(
        f"/api/v1/tablet/datasets/{publication.id}/download",
        HTTP_ACCEPT="application/octet-stream",
        **_authorization("not-a-credential"),
    )
    assert unauthorized.status_code == 403
    assert unauthorized["Content-Type"].startswith("application/problem+json")

    unknown = client.get(
        f"/api/v1/tablet/datasets/{uuid.uuid4()}/download",
        HTTP_ACCEPT="application/octet-stream",
        **_authorization(credential),
    )
    assert unknown.status_code == 404
    assert unknown["Content-Type"].startswith("application/problem+json")


def test_invalid_bearer_uses_rfc9457_problem_detail(api_context):
    client, _, _, _ = api_context
    response = client.get("/api/v1/tablet/status", **_authorization("not-a-credential"))

    assert response.status_code == 403
    assert response["Content-Type"].startswith("application/problem+json")
    assert response.json()["status"] == 403
    assert response.json()["request_id"]


def test_check_in_renews_an_active_installation(api_context):
    client, installation, credential, _ = api_context
    previous_expiry = installation.authorization_valid_until
    response = client.post("/api/v1/tablet/check-in", **_authorization(credential))

    installation.refresh_from_db()
    assert response.status_code == 200
    assert installation.authorization_valid_until > previous_expiry
    assert verify_credential(installation=installation, credential=credential)


@pytest.mark.parametrize(
    ("installation_status", "path", "expected_status"),
    [
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/status", 403),
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/check-in", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/configuration", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/configuration", 403),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/configuration", 403),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/configuration", 403),
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/manifest", 202),
        (AppInstallation.Status.STALE, "/api/v1/tablet/manifest", 403),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/manifest", 403),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/manifest", 403),
    ],
)
def test_installation_state_access_matrix(api_context, installation_status, path, expected_status):
    client, installation, credential, _ = api_context
    installation.status = installation_status
    installation.save(update_fields=("status",))

    if path.endswith("check-in"):
        response = client.post(path, **_authorization(credential))
    else:
        response = client.get(path, **_authorization(credential))

    assert response.status_code == expected_status
    if path.endswith("status") and expected_status == 200:
        assert response.json()["status"] == installation_status.lower()
        assert response.json()["purge_provisioned_data"] == (
            installation_status == AppInstallation.Status.REVOKED
        )


def test_openapi_schema_is_available():
    response = Client().get("/api/v1/schema/")

    assert response.status_code == 200
    assert "openapi: 3.1.0" in response.content.decode()


def test_download_content_negotiation_accepts_octet_stream():
    view = DownloadView()
    renderers = view.get_renderers()
    http_request = APIRequestFactory().get(
        "/api/v1/tablet/datasets/x/download", HTTP_ACCEPT="application/octet-stream"
    )
    request = Request(
        http_request,
        parsers=[],
        authenticators=[],
        negotiator=DefaultContentNegotiation(),
    )

    accepted_renderer, media_type = DefaultContentNegotiation().select_renderer(
        request, renderers, None
    )

    assert media_type == "application/octet-stream"
    assert accepted_renderer.media_type == "application/octet-stream"
