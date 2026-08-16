"""Cryptographic operations and canonical encodings for the FireDash protocol.

This module is intentionally framework-free: it mirrors exactly the wire
contract the server implements in ``apps/publications/hpke.py`` and
``apps/tablets/services.py`` so the fake client produces byte-identical
canonical inputs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

from tools.fake_ipad.errors import fail

HPKE_SUITE_NAME = "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM"
ADOPTION_PROTOCOL = "tablet-adoption-v1"
GRANT_PROTOCOL = "firedash-hpke-v1"

HPKE_SUITE = Suite(KEM.P256, KDF.HKDF_SHA256, AEAD.AES_128_GCM)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def strict_b64(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str):
        fail(f"{label}: expected Base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        fail(f"{label}: invalid strict Base64: {exc}")
    raise AssertionError("unreachable")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def challenge_proof(nonce: bytes, context: bytes) -> bytes:
    """HMAC-SHA-256(key=nonce, message=context), matching the server contract."""
    proof = hmac.new(nonce, context, hashlib.sha256).digest()
    if len(proof) != 32:
        fail("Internal HMAC-SHA256 length error")
    return proof


def ed25519_from_bytes(raw: bytes) -> ed25519.Ed25519PublicKey:
    if len(raw) != 32:
        fail(f"Ed25519 public key must be 32 bytes, got {len(raw)}")
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        fail(f"AES-256-GCM decrypt/authentication failed: {type(exc).__name__}: {exc}")
    raise AssertionError("unreachable")


@dataclass
class CryptoState:
    """The long-lived P-256 HPKE identity a physical installation persists."""

    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> CryptoState:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: bytes) -> CryptoState:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            fail("Stored private key is not an EC private key")
        if not isinstance(key.curve, ec.SECP256R1):
            fail("Stored private key is not P-256")
        return cls(key)

    def private_pem(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def public_bytes(self) -> bytes:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        if len(raw) != 65 or raw[0] != 0x04:
            fail("Generated P-256 public key is not a 65-byte uncompressed X9.62 point")
        return raw

    def public_key_b64(self) -> str:
        return base64.b64encode(self.public_bytes()).decode("ascii")

    def fingerprint(self) -> str:
        return sha256_hex(self.public_bytes())

    def hpke_open(self, encrypted: bytes, *, info: bytes) -> bytes:
        """Open a fixed-suite HPKE message (``enc || ciphertext``)."""
        try:
            return HPKE_SUITE.decrypt(encrypted, self.private_key, info=info)
        except Exception as exc:
            fail(f"HPKE open failed: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable")
