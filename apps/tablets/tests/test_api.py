import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import ApiVersionCompatibilityPolicy, SystemRole
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE, serialize_p256_public_key
from apps.publications.manifests import request_manifest
from apps.publications.models import (
    DatasetKeyGrant,
    DatasetPublication,
    DatasetScopeState,
    SignedManifest,
)
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
    tablet = Tablet.objects.create(
        department=department, display_name="Tablet", status=Tablet.Status.ACTIVE
    )
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
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=serialize_p256_public_key(key.public_key()),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=3),
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
    historical_public_key = (
        Ed25519PrivateKey.from_private_bytes(b"h" * 32)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    signing_ring_path = tmp_path / "signing-public-ring.json"
    signing_ring_path.write_text(
        json.dumps(
            {
                "keys": {
                    "0": base64.b64encode(historical_public_key).decode("ascii"),
                    "1": base64.b64encode(signing_public_path.read_bytes()).decode("ascii"),
                }
            }
        ),
        encoding="ascii",
    )
    with override_settings(PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=signing_ring_path):
        signing_key = client.get("/api/v1/tablet/signing-keys/1", **_authorization(credential))
    assert signing_key.status_code == 200
    assert signing_key.json() == {
        "algorithm": "Ed25519",
        "version": "1",
        "public_key": base64.b64encode(signing_public_path.read_bytes()).decode("ascii"),
    }
    historical_key = client.get("/api/v1/tablet/signing-keys/0", **_authorization(credential))
    assert historical_key.status_code == 200
    assert historical_key.json() == {
        "algorithm": "Ed25519",
        "version": "0",
        "public_key": base64.b64encode(historical_public_key).decode("ascii"),
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


def test_check_in_does_not_renew_an_active_installation_with_more_than_48_hours_remaining(
    api_context,
):
    client, installation, credential, _ = api_context
    previous_expiry = installation.authorization_valid_until
    response = client.post("/api/v1/tablet/check-in", **_authorization(credential))

    installation.refresh_from_db()
    assert response.status_code == 200
    assert installation.authorization_valid_until == previous_expiry
    assert installation.last_successful_check_in_at is not None
    assert verify_credential(installation=installation, credential=credential)


def test_check_in_reports_upgrade_before_lease_renewal_but_accepts_new_version_telemetry(
    api_context,
):
    client, installation, credential, _ = api_context
    actor = installation.tablet.department.created_by
    assert actor is not None
    ApiVersionCompatibilityPolicy.objects.create(
        api_major=1, minimum_app_version="1.0.1", updated_by=actor
    )
    previous_expiry = installation.authorization_valid_until

    blocked = client.post("/api/v1/tablet/check-in", **_authorization(credential))
    installation.refresh_from_db()
    assert blocked.status_code == 426
    assert blocked.json()["code"] == "client_update_required"
    assert blocked.json()["minimum_app_version"] == "1.0.1"
    assert installation.authorization_valid_until == previous_expiry

    upgraded = client.post(
        "/api/v1/tablet/check-in",
        HTTP_X_FIREDASH_APP_VERSION="1.0.1",
        HTTP_X_FIREDASH_APP_BUILD="57",
        **_authorization(credential),
    )
    installation.refresh_from_db()
    assert upgraded.status_code == 200
    assert installation.app_version == "1.0.1"
    assert installation.app_build == 57


def test_version_only_header_preserves_matching_build_and_clears_changed_build(api_context):
    client, installation, credential, _ = api_context
    installation.app_build = 7
    installation.save(update_fields=("app_build",))
    same = client.post(
        "/api/v1/tablet/check-in",
        HTTP_X_FIREDASH_APP_VERSION="1.0.0",
        **_authorization(credential),
    )
    installation.refresh_from_db()
    assert same.status_code == 200
    assert installation.app_build == 7
    changed = client.post(
        "/api/v1/tablet/check-in",
        HTTP_X_FIREDASH_APP_VERSION="1.0.1",
        **_authorization(credential),
    )
    installation.refresh_from_db()
    assert changed.status_code == 200
    assert installation.app_build is None


def test_api_compatibility_policy_requires_system_administrator(db):
    actor = User.objects.create_user("system-policy@example.test", "System", "safe-password")
    from apps.authorization.services import set_api_version_compatibility_policy

    with pytest.raises(PermissionDenied):
        set_api_version_compatibility_policy(actor=actor, api_major=1, minimum_app_version="1.0.0")
    SystemRole.objects.create(user=actor)
    policy = set_api_version_compatibility_policy(
        actor=actor, api_major=1, minimum_app_version="1.0.0"
    )
    assert policy.minimum_app_version == "1.0.0"


def test_refresh_tops_up_an_active_installation_without_delivery_work(api_context):
    client, installation, credential, _ = api_context
    old_expiry = installation.authorization_valid_until
    request_manifest(installation=installation)
    manifest_count = SignedManifest.objects.filter(app_installation=installation).count()
    grant_count = DatasetKeyGrant.objects.filter(app_installation=installation).count()

    unauthenticated = client.post("/api/v1/tablet/refresh")
    assert unauthenticated.status_code == 403

    response = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    installation.refresh_from_db()

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["server_time"]
    assert response.json()["authorization_valid_until"]
    assert installation.last_successful_check_in_at is not None
    assert (
        timedelta(days=6)
        < installation.authorization_valid_until - timezone.now()
        <= timedelta(days=7)
    )
    assert installation.authorization_valid_until > old_expiry
    assert SignedManifest.objects.filter(app_installation=installation).count() == manifest_count
    assert DatasetKeyGrant.objects.filter(app_installation=installation).count() == grant_count
    from apps.audit.models import AuditEvent

    event = AuditEvent.objects.get(action="tablet.self_refreshed", target_uuid=installation.id)
    assert event.metadata["old_expiry"]
    assert event.metadata["new_expiry"]

    refreshed_expiry = installation.authorization_valid_until
    second_response = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    installation.refresh_from_db()
    assert second_response.status_code == 200
    assert (
        refreshed_expiry
        <= installation.authorization_valid_until
        < refreshed_expiry + timedelta(minutes=1)
    )

    # The changed expiry is observed only by the existing manifest request path.
    request_manifest(installation=installation)
    assert (
        SignedManifest.objects.filter(app_installation=installation).count() == manifest_count + 1
    )


def test_refresh_honors_policy_without_shortening_a_longer_lease(api_context):
    client, installation, credential, _ = api_context
    department = installation.tablet.department
    department.tablet_lease_days = 3
    department.save(update_fields=("tablet_lease_days",))

    response = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    installation.refresh_from_db()
    assert response.status_code == 200
    assert (
        timedelta(days=2)
        < installation.authorization_valid_until - timezone.now()
        <= timedelta(days=3)
    )

    longer_expiry = timezone.now() + timedelta(days=10)
    installation.authorization_valid_until = longer_expiry
    installation.save(update_fields=("authorization_valid_until",))
    response = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    installation.refresh_from_db()
    assert response.status_code == 200
    assert installation.authorization_valid_until == longer_expiry


def test_refresh_rejects_expired_or_non_operational_installations(api_context):
    client, installation, credential, _ = api_context
    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))

    expired = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    installation.refresh_from_db()
    assert expired.status_code == 403
    assert installation.status == AppInstallation.Status.ACTIVE

    installation.authorization_valid_until = timezone.now() + timedelta(days=1)
    installation.status = AppInstallation.Status.ACTIVE
    installation.tablet.active = False
    installation.tablet.save(update_fields=("active",))
    installation.save(update_fields=("authorization_valid_until", "status"))
    inactive = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    assert inactive.status_code == 403

    installation.tablet.active = True
    installation.tablet.status = Tablet.Status.REMOVED
    installation.tablet.save(update_fields=("active", "status"))
    removed = client.post("/api/v1/tablet/refresh", **_authorization(credential))
    assert removed.status_code == 403


@pytest.mark.parametrize(
    ("installation_status", "path", "expected_status"),
    [
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/status", 200),
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/check-in", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/check-in", 403),
        (AppInstallation.Status.ACTIVE, "/api/v1/tablet/refresh", 200),
        (AppInstallation.Status.STALE, "/api/v1/tablet/refresh", 403),
        (AppInstallation.Status.REVOKED, "/api/v1/tablet/refresh", 403),
        (AppInstallation.Status.REPLACED, "/api/v1/tablet/refresh", 403),
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

    if path.endswith(("check-in", "refresh")):
        response = client.post(path, **_authorization(credential))
    else:
        response = client.get(path, **_authorization(credential))

    assert response.status_code == expected_status
    if path.endswith("status") and expected_status == 200:
        assert response.json()["status"] == installation_status.lower()
        assert response.json()["purge_provisioned_data"] == (
            installation_status in (AppInstallation.Status.REVOKED, AppInstallation.Status.REPLACED)
        )


def test_openapi_schema_is_available():
    response = Client().get("/api/v1/schema/")

    assert response.status_code == 200
    assert "openapi: 3.1.0" in response.content.decode()


def test_download_openapi_contract_has_no_drf_format_query_parameter():
    response = Client().get("/api/v1/schema/")

    schema = yaml.safe_load(response.content)
    parameters = schema["paths"]["/api/v1/tablet/datasets/{publication_id}/download"]["get"].get(
        "parameters", []
    )
    assert not any(
        parameter["name"] == "format" and parameter["in"] == "query" for parameter in parameters
    )
    response_content = schema["paths"]["/api/v1/tablet/datasets/{publication_id}/download"]["get"][
        "responses"
    ]["200"]["content"]
    assert response_content == {
        "application/octet-stream": {"schema": {"format": "binary", "type": "string"}}
    }


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
