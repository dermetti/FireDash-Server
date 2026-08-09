"""Fixed-suite, single-shot HPKE helpers for FireDash protocol bindings."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

HPKE_CIPHERSUITE = "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM"
HPKE_PROTOCOL = "firedash-hpke-v1"
_P256_PUBLIC_KEY_LENGTH = 65
_DATASET_TYPE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_SUITE = Suite(KEM.P256, KDF.HKDF_SHA256, AEAD.AES_128_GCM)


class HPKEError(ValueError):
    """A public HPKE input or authentication failure."""


@dataclass(frozen=True)
class HPKEContext:
    """All identifiers bound through RFC 9180's `info` parameter."""

    publication_id: uuid.UUID
    installation_id: uuid.UUID
    tablet_id: uuid.UUID
    department_id: uuid.UUID
    station_id: uuid.UUID | None
    dataset_type_code: str
    version_number: int
    schema_version: int
    ciphertext_sha256: str

    def info(self) -> bytes:
        if not _DATASET_TYPE_CODE.fullmatch(self.dataset_type_code):
            raise HPKEError("Dataset type code is not canonical.")
        if self.version_number < 1 or self.schema_version < 1:
            raise HPKEError("Publication versions must be positive.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.ciphertext_sha256):
            raise HPKEError("Ciphertext SHA-256 is not canonical.")
        return json.dumps(
            {
                "ciphertext_sha256": self.ciphertext_sha256,
                "installation_id": str(self.installation_id),
                "protocol": HPKE_PROTOCOL,
                "publication_id": str(self.publication_id),
                "schema_version": self.schema_version,
                "scope": {
                    "dataset_type_code": self.dataset_type_code,
                    "department_id": str(self.department_id),
                    "station_id": str(self.station_id) if self.station_id else None,
                },
                "tablet_id": str(self.tablet_id),
                "version_number": self.version_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")


class HPKEInfoContext(Protocol):
    def info(self) -> bytes: ...


def parse_p256_public_key(encoded: bytes) -> ec.EllipticCurvePublicKey:
    """Parse only the RFC 9180 P-256 uncompressed-point encoding."""
    if len(encoded) != _P256_PUBLIC_KEY_LENGTH or encoded[:1] != b"\x04":
        raise HPKEError("P-256 public key must be a 65-byte uncompressed point.")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encoded)
    except ValueError as error:
        raise HPKEError("P-256 public key is invalid.") from error


def serialize_p256_public_key(public_key: ec.EllipticCurvePublicKey) -> bytes:
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise HPKEError("HPKE requires a P-256 public key.")
    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def public_key_fingerprint(public_key: ec.EllipticCurvePublicKey) -> str:
    """Return the stable SHA-256 fingerprint of canonical P-256 key bytes."""
    return hashlib.sha256(serialize_p256_public_key(public_key)).hexdigest()


def hpke_seal(
    *, plaintext: bytes, recipient_public_key: ec.EllipticCurvePublicKey, context: HPKEInfoContext
) -> tuple[bytes, bytes]:
    """Seal one message and return RFC 9180 `enc` and AEAD ciphertext separately."""
    encoded = _SUITE.encrypt(plaintext, recipient_public_key, info=context.info())
    return encoded[: KEM.P256.enc_length()], encoded[KEM.P256.enc_length() :]


def hpke_open(
    *,
    encapsulated_key: bytes,
    ciphertext: bytes,
    recipient_private_key: ec.EllipticCurvePrivateKey,
    context: HPKEInfoContext,
) -> bytes:
    """Open one fixed-suite message without exposing authentication details."""
    if not isinstance(recipient_private_key.curve, ec.SECP256R1):
        raise HPKEError("HPKE requires a P-256 private key.")
    if len(encapsulated_key) != KEM.P256.enc_length():
        raise HPKEError("HPKE encapsulated key has an invalid length.")
    try:
        return _SUITE.decrypt(
            encapsulated_key + ciphertext, recipient_private_key, info=context.info()
        )
    except (InvalidTag, ValueError) as error:
        raise HPKEError("HPKE authentication failed.") from error
