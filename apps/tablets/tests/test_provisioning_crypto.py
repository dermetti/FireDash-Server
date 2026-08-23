import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from apps.publications.hpke import (
    hpke_open,
    hpke_seal,
    public_key_fingerprint,
    serialize_p256_public_key,
)
from apps.tablets.models import AppInstallation
from apps.tablets.services import (
    ADOPTION_PROTOCOL,
    AdoptionChallengeContext,
    canonical_protocol_datetime,
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
        mode="adoption",
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
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "b" * 64,
        timezone.now() + timedelta(minutes=5),
        "adoption",
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
        context.mode,
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


def test_adoption_context_reconstructed_from_preview_values_opens_challenge():
    private_key = ec.generate_private_key(ec.SECP256R1())
    context = AdoptionChallengeContext(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "c" * 64,
        timezone.now() + timedelta(minutes=5),
        "adoption",
    )
    preview = {
        "adoption_request_id": str(context.adoption_request_id),
        "expires_at": canonical_protocol_datetime(context.expires_at),
        "tablet_id": str(context.tablet_id),
        "hpke_ciphersuite": "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        "hpke_public_key_fingerprint": context.public_key_fingerprint,
        "installation_uuid": str(context.installation_uuid),
        "mode": context.mode,
        "protocol": ADOPTION_PROTOCOL,
    }
    reconstructed = AdoptionChallengeContext(
        adoption_request_id=uuid.UUID(preview["adoption_request_id"]),
        installation_uuid=uuid.UUID(preview["installation_uuid"]),
        tablet_id=uuid.UUID(preview["tablet_id"]),
        public_key_fingerprint=preview["hpke_public_key_fingerprint"],
        expires_at=context.expires_at,
        mode=preview["mode"],
    )

    encapsulated_key, ciphertext = hpke_seal(
        plaintext=b"n" * 32, recipient_public_key=private_key.public_key(), context=context
    )

    assert reconstructed.info() == context.info()
    assert (
        hpke_open(
            encapsulated_key=encapsulated_key,
            ciphertext=ciphertext,
            recipient_private_key=private_key,
            context=reconstructed,
        )
        == b"n" * 32
    )


def test_adoption_context_has_a_frozen_mode_bound_canonical_encoding():
    context = AdoptionChallengeContext(
        adoption_request_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        installation_uuid=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        tablet_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        public_key_fingerprint="a" * 64,
        expires_at=datetime(2026, 8, 9, 12, 39, 56, 789012, tzinfo=UTC),
        mode="adoption",
    )

    assert context.info() == (
        b'{"adoption_request_id":"11111111-1111-1111-1111-111111111111",'
        b'"expires_at":"2026-08-09T12:39:56.789012Z",'
        b'"hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",'
        b'"hpke_public_key_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"installation_uuid":"22222222-2222-2222-2222-222222222222",'
        b'"mode":"adoption","protocol":"tablet-adoption-v1",'
        b'"tablet_id":"33333333-3333-3333-3333-333333333333"}'
    )


def test_credential_verifier_is_constant_time_hash_comparison(settings):
    credential = generate_credential()
    installation = AppInstallation(
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest()
    )

    assert verify_credential(installation=installation, credential=credential)
    assert not verify_credential(installation=installation, credential=generate_credential())


# --- canonical protocol datetime ---------------------------------------------


def test_canonical_protocol_datetime_utc_input():
    value = datetime(2026, 8, 14, 15, 0, 0, 123456, tzinfo=UTC)
    assert canonical_protocol_datetime(value) == "2026-08-14T15:00:00.123456Z"


def test_canonical_protocol_datetime_normalizes_non_utc_offset():
    value = datetime(2026, 8, 14, 17, 0, 0, 123456, tzinfo=dt_timezone(timedelta(hours=2)))
    assert canonical_protocol_datetime(value) == "2026-08-14T15:00:00.123456Z"


def test_canonical_protocol_datetime_rejects_naive_datetime():
    with pytest.raises(ValueError):
        canonical_protocol_datetime(datetime(2026, 8, 14, 15, 0, 0, 123456))


def test_canonical_protocol_datetime_is_deterministic_for_equivalent_instants():
    utc_value = datetime(2026, 8, 14, 15, 0, 0, 123456, tzinfo=UTC)
    offset_value = datetime(2026, 8, 14, 17, 0, 0, 123456, tzinfo=dt_timezone(timedelta(hours=2)))
    assert canonical_protocol_datetime(utc_value) == canonical_protocol_datetime(offset_value)


def test_canonical_protocol_datetime_never_uses_offset_suffix():
    value = datetime(2026, 8, 14, 15, 0, 0, 123456, tzinfo=UTC)
    assert canonical_protocol_datetime(value).endswith("Z")
    assert "+00:00" not in canonical_protocol_datetime(value)


# --- HPKE framing / fingerprint contract --------------------------------------


def test_adoption_hpke_wire_framing_lengths():
    private_key = ec.generate_private_key(ec.SECP256R1())
    context = AdoptionChallengeContext(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "d" * 64,
        timezone.now() + timedelta(minutes=5),
        "adoption",
    )
    nonce = b"x" * 32
    encapsulated_key, ciphertext = hpke_seal(
        plaintext=nonce, recipient_public_key=private_key.public_key(), context=context
    )

    assert len(encapsulated_key) == 65
    assert encapsulated_key[:1] == b"\x04"
    assert len(ciphertext) == 48  # 32-byte plaintext + 16-byte GCM tag
    assert len(encapsulated_key + ciphertext) == 113
    assert len(nonce) == 32
    assert (
        hpke_open(
            encapsulated_key=encapsulated_key,
            ciphertext=ciphertext,
            recipient_private_key=private_key,
            context=context,
        )
        == nonce
    )


def test_public_key_fingerprint_is_sha256_of_65_byte_uncompressed_point():
    private_key = ec.generate_private_key(ec.SECP256R1())
    encoded = serialize_p256_public_key(private_key.public_key())
    assert len(encoded) == 65
    assert encoded[:1] == b"\x04"
    assert public_key_fingerprint(private_key.public_key()) == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    ("microsecond", "expected"),
    (
        (0, "2026-08-14T15:00:00Z"),
        (1, "2026-08-14T15:00:00.000001Z"),
        (120000, "2026-08-14T15:00:00.120000Z"),
        (999999, "2026-08-14T15:00:00.999999Z"),
    ),
)
def test_canonical_protocol_datetime_fixed_fractional_cases(microsecond, expected):
    value = datetime(2026, 8, 14, 15, 0, 0, microsecond, tzinfo=UTC)
    assert canonical_protocol_datetime(value) == expected


def test_adoption_response_expires_at_matches_context_bytes():
    # The preview response ``expires_at`` string must byte-match the value bound
    # into AdoptionChallengeContext.info(); semantic equality is not sufficient.
    expires_at = datetime(2026, 8, 14, 15, 0, 0, 120000, tzinfo=UTC)
    context = AdoptionChallengeContext(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "e" * 64,
        expires_at,
        "adoption",
    )
    response_expires_at = canonical_protocol_datetime(expires_at)
    assert f'"expires_at":"{response_expires_at}"'.encode("ascii") in context.info()
