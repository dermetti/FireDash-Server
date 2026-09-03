"""API-boundary regression tests for adoption HPKE canonical timestamps.

These prove the exact production failure path: the DRF wire value for
``expires_at`` must byte-match the timestamp bound into the server's HPKE
``info``, so an external client can reconstruct the context from the response
alone and open the challenge.
"""

import base64
import hashlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE, hpke_open, serialize_p256_public_key
from apps.tablets.models import AdoptionRequest, AppInstallation, Tablet
from apps.tablets.services import create_adoption_invitation

ADOPTION_PREVIEW = "/api/v1/adoption/preview"
ADOPTION_COMPLETE = "/api/v1/adoption/complete"


class _ClientContext:
    """Minimal HPKE ``info`` provider mirroring an external client reconstruction."""

    def __init__(self, info: bytes) -> None:
        self._info = info

    def info(self) -> bytes:
        return self._info


def _client_context_bytes(preview: dict[str, object], installation_uuid: str) -> bytes:
    """Rebuild the canonical context exclusively from the wire response values."""
    return json.dumps(
        {
            "adoption_request_id": preview["adoption_request_id"],
            "expires_at": preview["expires_at"],
            "hpke_ciphersuite": preview["hpke_ciphersuite"],
            "hpke_public_key_fingerprint": preview["hpke_public_key_fingerprint"],
            "installation_uuid": installation_uuid,
            "mode": preview["mode"],
            "protocol": preview["protocol"],
            "tablet_id": preview["tablet_id"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _open_challenge(encrypted_challenge: str, private_key, info: bytes) -> bytes:
    decoded = base64.b64decode(encrypted_challenge)
    return hpke_open(
        encapsulated_key=decoded[:65],
        ciphertext=decoded[65:],
        recipient_private_key=private_key,
        context=_ClientContext(info),
    )


@pytest.fixture
def crypto_api_context(db):
    now = timezone.now()
    user = User.objects.create_user("cryptoapi@example.test", "Crypto API", "safe-password")
    department = Department.objects.create(name="Crypto Dept", short_code="CRY", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Engine 1"
    )
    tablet = Tablet.objects.create(department=department, display_name="Tablet")
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=now, created_by=user
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    installation_uuid = uuid.uuid4()
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=installation_uuid,
        credential_hash="a" * 64,
        status=AppInstallation.Status.STALE,
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
    return SimpleNamespace(
        user=user,
        tablet=tablet,
        installation=installation,
        installation_uuid=installation_uuid,
        private_key=private_key,
    )


@pytest.mark.django_db(transaction=True)
def test_adoption_preview_wire_timestamp_matches_hpke_context(crypto_api_context):
    ctx = crypto_api_context
    client = Client()
    private_key = ec.generate_private_key(ec.SECP256R1())
    installation_uuid = uuid.uuid4()
    _, token = create_adoption_invitation(actor=ctx.user, tablet=ctx.tablet)
    public_key = serialize_p256_public_key(private_key.public_key())

    response = client.post(
        ADOPTION_PREVIEW,
        data=json.dumps(
            {
                "token": token,
                "installation_uuid": str(installation_uuid),
                "app_version": "1.0.0",
                "hpke_public_key": base64.b64encode(public_key).decode("ascii"),
                "hpke_ciphersuite": HPKE_CIPHERSUITE,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 201
    preview = response.json()
    assert preview["mode"] == "adoption"
    assert preview["expires_at"].endswith("Z")
    assert "+00:00" not in preview["expires_at"]

    info = _client_context_bytes(preview, str(installation_uuid))
    plaintext = _open_challenge(preview["encrypted_challenge"], private_key, info)
    assert len(plaintext) == 32

    request = AdoptionRequest.objects.get(pk=preview["adoption_request_id"])
    assert hashlib.sha256(info).hexdigest() == request.canonical_context_hash


def test_completion_replay_rotates_only_the_original_installation_and_rejects_new_preview(
    crypto_api_context,
):
    """A local-finalization retry is exact completion replay, never a new preview."""
    ctx = crypto_api_context
    client = Client()
    installation_uuid = uuid.uuid4()
    public_key = serialize_p256_public_key(ctx.private_key.public_key())

    def preview(token: str):
        response = client.post(
            ADOPTION_PREVIEW,
            data=json.dumps(
                {
                    "token": token,
                    "installation_uuid": str(installation_uuid),
                    "app_version": "1.0.0",
                    "hpke_public_key": base64.b64encode(public_key).decode("ascii"),
                    "hpke_ciphersuite": HPKE_CIPHERSUITE,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 201
        return response.json()

    _, first_token = create_adoption_invitation(actor=ctx.user, tablet=ctx.tablet)
    first_preview = preview(first_token)
    first_request = AdoptionRequest.objects.get(pk=first_preview["adoption_request_id"])
    completion = {
        "adoption_request_id": str(first_request.id),
        "challenge_response": base64.b64encode(first_request.expected_hmac_digest).decode("ascii"),
        "confirmed": True,
    }

    first = client.post(
        ADOPTION_COMPLETE, data=json.dumps(completion), content_type="application/json"
    )
    assert first.status_code == 201
    first_payload = first.json()
    first_request.refresh_from_db()
    original_deadline = first_request.completion_replay_valid_until
    assert original_deadline is not None

    replay = client.post(
        ADOPTION_COMPLETE, data=json.dumps(completion), content_type="application/json"
    )
    assert replay.status_code == 201
    replay_payload = replay.json()
    assert replay_payload["installation_id"] == first_payload["installation_id"]
    assert replay_payload["credential"] != first_payload["credential"]

    repeated_replay = client.post(
        ADOPTION_COMPLETE, data=json.dumps(completion), content_type="application/json"
    )
    assert repeated_replay.status_code == 201
    first_request.refresh_from_db()
    assert first_request.completion_replay_valid_until == original_deadline
    installation = AppInstallation.objects.get(pk=first_payload["installation_id"])
    credential_hash_before_invalid_retry = installation.credential_hash

    # This is the former 500 path: local state retries provisioning with a
    # newly issued invitation but the installation UUID already belongs to the
    # server-completed adoption.  It must not be mistaken for recovery.
    _, replacement_token = create_adoption_invitation(actor=ctx.user, tablet=ctx.tablet)
    replacement_preview = preview(replacement_token)
    replacement_request = AdoptionRequest.objects.get(
        pk=replacement_preview["adoption_request_id"]
    )
    invalid_retry = client.post(
        ADOPTION_COMPLETE,
        data=json.dumps(
            {
                "adoption_request_id": str(replacement_request.id),
                "challenge_response": base64.b64encode(
                    replacement_request.expected_hmac_digest
                ).decode("ascii"),
                "confirmed": True,
            }
        ),
        content_type="application/json",
    )

    assert invalid_retry.status_code == 403
    assert invalid_retry["Content-Type"].startswith("application/problem+json")
    assert invalid_retry.json()["code"] == "invalid_request"
    installation.refresh_from_db()
    replacement_request.refresh_from_db()
    assert installation.status == AppInstallation.Status.ACTIVE
    assert installation.credential_hash == credential_hash_before_invalid_retry
    assert AppInstallation.objects.filter(installation_uuid=installation_uuid).count() == 1
    assert replacement_request.completed_at is None
