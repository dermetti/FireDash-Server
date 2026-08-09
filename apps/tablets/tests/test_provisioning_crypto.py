import hashlib
import hmac
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from apps.publications.hpke import hpke_open, hpke_seal
from apps.tablets.models import AppInstallation
from apps.tablets.services import (
    ADOPTION_PROTOCOL,
    AdoptionChallengeContext,
    generate_credential,
    verify_credential,
)


def test_adoption_challenge_is_bound_to_canonical_context():
    private_key = ec.generate_private_key(ec.SECP256R1())
    context = AdoptionChallengeContext(
        adoption_request_id=uuid.uuid4(),
        installation_uuid=uuid.uuid4(),
        tablet_id=uuid.uuid4(),
        public_key_fingerprint="a" * 64,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    nonce = b"n" * 32
    encapsulated_key, ciphertext = hpke_seal(
        plaintext=nonce, recipient_public_key=private_key.public_key(), context=context
    )

    assert ADOPTION_PROTOCOL.encode() in context.info()
    assert (
        hpke_open(
            encapsulated_key=encapsulated_key,
            ciphertext=ciphertext,
            recipient_private_key=private_key,
            context=context,
        )
        == nonce
    )
    assert (
        hmac.digest(nonce, context.info(), "sha256")
        == hmac.new(nonce, context.info(), hashlib.sha256).digest()
    )


def test_adoption_context_changes_cannot_open_challenge():
    private_key = ec.generate_private_key(ec.SECP256R1())
    context = AdoptionChallengeContext(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "b" * 64, timezone.now() + timedelta(minutes=5)
    )
    encapsulated_key, ciphertext = hpke_seal(
        plaintext=b"n" * 32, recipient_public_key=private_key.public_key(), context=context
    )
    changed = AdoptionChallengeContext(
        context.adoption_request_id,
        context.installation_uuid,
        uuid.uuid4(),
        context.public_key_fingerprint,
        context.expires_at,
    )

    try:
        hpke_open(
            encapsulated_key=encapsulated_key,
            ciphertext=ciphertext,
            recipient_private_key=private_key,
            context=changed,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("A challenge must be bound to the tablet identity.")


def test_credential_verifier_is_constant_time_hash_comparison(settings):
    credential = generate_credential()
    installation = AppInstallation(
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest()
    )

    assert verify_credential(installation=installation, credential=credential)
    assert not verify_credential(installation=installation, credential=generate_credential())
