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
