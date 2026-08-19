"""Emit a fully synthetic, self-contained adoption HPKE fixture for client tests.

Every value is test-only: a fresh P-256 keypair and a synthetic challenge are
generated on each run. Live installation keys, invitation tokens, or the server
SECRET_KEY are never used or exported.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand

from apps.publications.hpke import HPKE_CIPHERSUITE, hpke_seal, serialize_p256_public_key
from apps.tablets.services import (
    ADOPTION_PROTOCOL,
    AdoptionChallengeContext,
    canonical_protocol_datetime,
)


def _pem(private_key: ec.EllipticCurvePrivateKey) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


class Command(BaseCommand):
    help = "Emit a synthetic adoption HPKE fixture for iOS interoperability tests."

    def handle(self, *args, **options):
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = serialize_p256_public_key(private_key.public_key())
        fingerprint = hashlib.sha256(public_key).hexdigest()

        context = AdoptionChallengeContext(
            adoption_request_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            installation_uuid=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            tablet_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            public_key_fingerprint=fingerprint,
            expires_at=datetime(2030, 1, 9, 3, 4, 5, tzinfo=UTC),
            mode="adoption",
        )
        nonce = secrets.token_bytes(32)
        encapsulated_key, ciphertext = hpke_seal(
            plaintext=nonce, recipient_public_key=private_key.public_key(), context=context
        )
        info = context.info()
        expected_hmac = hmac.digest(nonce, info, "sha256")

        fixture = {
            "recipient_private_key_pem": _pem(private_key),
            "recipient_private_key_hex": private_key.private_numbers()
            .private_value.to_bytes(32, "big")
            .hex(),
            "recipient_public_key_hex": public_key.hex(),
            "recipient_public_key_fingerprint": fingerprint,
            "adoption_request_id": str(context.adoption_request_id),
            "installation_uuid": str(context.installation_uuid),
            "tablet_id": str(context.tablet_id),
            "expires_at": canonical_protocol_datetime(context.expires_at),
            "mode": context.mode,
            "protocol": ADOPTION_PROTOCOL,
            "hpke_ciphersuite": HPKE_CIPHERSUITE,
            "canonical_context_json": info.decode("ascii"),
            "canonical_context_hex": info.hex(),
            "canonical_context_sha256": hashlib.sha256(info).hexdigest(),
            "encrypted_challenge_hex": (encapsulated_key + ciphertext).hex(),
            "expected_nonce_hex": nonce.hex(),
            "expected_hmac_hex": expected_hmac.hex(),
        }
        self.stdout.write(json.dumps(fixture, indent=2, sort_keys=True))
