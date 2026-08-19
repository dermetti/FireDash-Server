"""The synthetic adoption fixture command must be self-consistent."""

import hashlib
import hmac
import io
import json
import uuid
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management import call_command

from apps.publications.hpke import hpke_open, serialize_p256_public_key
from apps.tablets.services import AdoptionChallengeContext


def test_emit_adoption_fixture_is_self_consistent():
    out = io.StringIO()
    call_command("emit_adoption_fixture", stdout=out)
    fixture = json.loads(out.getvalue())

    private_key = ec.derive_private_key(
        int.from_bytes(bytes.fromhex(fixture["recipient_private_key_hex"]), "big"),
        ec.SECP256R1(),
    )
    public_key = private_key.public_key()

    # Fingerprint = SHA-256(65-byte X9.62 uncompressed point).
    assert serialize_p256_public_key(public_key).hex() == fixture["recipient_public_key_hex"]
    assert (
        hashlib.sha256(bytes.fromhex(fixture["recipient_public_key_hex"])).hexdigest()
        == fixture["recipient_public_key_fingerprint"]
    )

    # Canonical context bytes are exactly the documented JSON.
    info = bytes.fromhex(fixture["canonical_context_hex"])
    assert info.decode("ascii") == fixture["canonical_context_json"]
    assert hashlib.sha256(info).hexdigest() == fixture["canonical_context_sha256"]

    # Reconstruct the context from public fields and open the challenge.
    expires_at = datetime.fromisoformat(fixture["expires_at"].replace("Z", "+00:00"))
    context = AdoptionChallengeContext(
        adoption_request_id=uuid.UUID(fixture["adoption_request_id"]),
        installation_uuid=uuid.UUID(fixture["installation_uuid"]),
        tablet_id=uuid.UUID(fixture["tablet_id"]),
        public_key_fingerprint=fixture["recipient_public_key_fingerprint"],
        expires_at=expires_at,
        mode=fixture["mode"],
    )
    assert context.info() == info

    encrypted = bytes.fromhex(fixture["encrypted_challenge_hex"])
    assert len(encrypted) == 113
    nonce = hpke_open(
        encapsulated_key=encrypted[:65],
        ciphertext=encrypted[65:],
        recipient_private_key=private_key,
        context=context,
    )
    assert nonce.hex() == fixture["expected_nonce_hex"]
    assert hmac.digest(nonce, info, "sha256").hex() == fixture["expected_hmac_hex"]
