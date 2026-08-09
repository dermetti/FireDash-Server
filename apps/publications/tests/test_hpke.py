import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    HPKEContext,
    HPKEError,
    hpke_open,
    hpke_seal,
    parse_p256_public_key,
    public_key_fingerprint,
    serialize_p256_public_key,
)


@pytest.fixture
def contract():
    return json.loads((Path(__file__).parent / "fixtures" / "hpke_contract.json").read_text())


@pytest.fixture
def context(contract):
    values = contract["context"]
    return HPKEContext(
        publication_id=uuid.UUID(values["publication_id"]),
        installation_id=uuid.UUID(values["installation_id"]),
        tablet_id=uuid.UUID(values["tablet_id"]),
        department_id=uuid.UUID(values["department_id"]),
        station_id=uuid.UUID(values["station_id"]),
        dataset_type_code=values["dataset_type_code"],
        version_number=values["version_number"],
        schema_version=values["schema_version"],
        ciphertext_sha256=values["ciphertext_sha256"],
    )


def test_context_encoding_and_fixed_suite_are_frozen(contract, context):
    assert HPKE_CIPHERSUITE == contract["ciphersuite"]
    assert context.info() == contract["canonical_info"].encode("ascii")


def test_p256_key_encoding_and_fingerprint_are_canonical():
    private_key = ec.derive_private_key(7, ec.SECP256R1())
    encoded = serialize_p256_public_key(private_key.public_key())

    assert len(encoded) == 65
    assert serialize_p256_public_key(parse_p256_public_key(encoded)) == encoded
    assert public_key_fingerprint(private_key.public_key()) == public_key_fingerprint(
        parse_p256_public_key(encoded)
    )
    with pytest.raises(HPKEError):
        parse_p256_public_key(encoded[1:])
    with pytest.raises(HPKEError):
        parse_p256_public_key(b"\x04" + b"\x00" * 64)


def test_rfc9180_p256_base_vector_uses_canonical_key_encodings(contract):
    vector = contract["rfc9180_base_vector"]
    private_key = ec.derive_private_key(int(vector["private_key"], 16), ec.SECP256R1())

    assert serialize_p256_public_key(private_key.public_key()).hex() == vector["public_key"]
    assert (
        serialize_p256_public_key(parse_p256_public_key(bytes.fromhex(vector["enc"]))).hex()
        == vector["enc"]
    )


def test_hpke_round_trip_and_all_bound_context_values_fail_when_changed(context):
    private_key = ec.generate_private_key(ec.SECP256R1())
    encapsulated_key, ciphertext = hpke_seal(
        plaintext=b"wrapped content key",
        recipient_public_key=private_key.public_key(),
        context=context,
    )

    assert (
        hpke_open(
            encapsulated_key=encapsulated_key,
            ciphertext=ciphertext,
            recipient_private_key=private_key,
            context=context,
        )
        == b"wrapped content key"
    )
    assert len(encapsulated_key) == 65

    changed_contexts = (
        HPKEContext(**{**context.__dict__, "publication_id": uuid.uuid4()}),
        HPKEContext(**{**context.__dict__, "installation_id": uuid.uuid4()}),
        HPKEContext(**{**context.__dict__, "tablet_id": uuid.uuid4()}),
        HPKEContext(**{**context.__dict__, "department_id": uuid.uuid4()}),
        HPKEContext(**{**context.__dict__, "station_id": None}),
        HPKEContext(**{**context.__dict__, "dataset_type_code": "department_hydrants"}),
        HPKEContext(**{**context.__dict__, "version_number": 2}),
        HPKEContext(**{**context.__dict__, "schema_version": 2}),
        HPKEContext(**{**context.__dict__, "ciphertext_sha256": "a" * 64}),
    )
    for changed_context in changed_contexts:
        with pytest.raises(HPKEError, match="authentication failed"):
            hpke_open(
                encapsulated_key=encapsulated_key,
                ciphertext=ciphertext,
                recipient_private_key=private_key,
                context=changed_context,
            )


def test_hpke_rejects_wrong_key_and_tampered_data(context):
    private_key = ec.generate_private_key(ec.SECP256R1())
    encapsulated_key, ciphertext = hpke_seal(
        plaintext=b"wrapped content key",
        recipient_public_key=private_key.public_key(),
        context=context,
    )
    for changed_encapsulated_key, changed_ciphertext, changed_private_key in (
        (encapsulated_key, ciphertext, ec.generate_private_key(ec.SECP256R1())),
        (bytes([encapsulated_key[0] ^ 1]) + encapsulated_key[1:], ciphertext, private_key),
        (encapsulated_key, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), private_key),
    ):
        with pytest.raises(HPKEError, match="authentication failed"):
            hpke_open(
                encapsulated_key=changed_encapsulated_key,
                ciphertext=changed_ciphertext,
                recipient_private_key=changed_private_key,
                context=context,
            )
