# Adoption / reactivation HPKE v1

This is the byte-for-byte interoperability contract for the FireDash adoption
and reactivation challenge. The server implementation is `apps/tablets/services.py`
(`AdoptionChallengeContext`, `canonical_protocol_datetime`) and
`apps/publications/hpke.py`.

## HPKE suite and mode

- KEM: `DHKEM(P-256, HKDF-SHA256)`
- KDF: `HKDF-SHA256`
- AEAD: `AES-128-GCM`
- Mode: RFC 9180 **Base mode** (`mode_base`). Not `auth`, not `psk`, not `auth_psk`.
- PSK: **absent**. PSK ID: **absent**. Sender authentication key: **absent**.
- AAD: **empty** (zero-length).
- `info`: the exact canonical `AdoptionChallengeContext` bytes (below).

The ciphersuite string is exactly:

    DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM

## Recipient public key

- Representation: 65-byte X9.62 **uncompressed** point, prefixed with `0x04`.
- The server fingerprint is:

      SHA-256(65-byte X9.62 uncompressed P-256 public key)

  expressed as 64 lowercase hex characters. The client must derive the same
  fingerprint from the public key corresponding to the private key it uses to
  open the challenge. A fingerprint mismatch means the wrong keypair was used.

## Challenge plaintext

- 32 cryptographically random bytes (never persisted on the server).

## Wire payload framing

- `hpke_seal` returns `enc || ciphertext`.
- `enc`: the first **65 bytes** (the HPKE encapsulated P-256 point).
- `ciphertext`: the remaining **48 bytes** = 32-byte plaintext + 16-byte AES-GCM
  authentication tag.
- Total complete encrypted challenge length: **65 + 48 = 113 bytes**.
- The AEAD nonce is derived internally by HPKE; the wire payload carries **no**
  separate 12-byte AES-GCM nonce and must **not** be parsed as a standalone
  CryptoKit `AES.GCM.SealedBox`.

## Canonical context (`info`)

`AdoptionChallengeContext.info()` is the exact UTF-8/ASCII JSON, with sorted keys
and compact separators (`sort_keys=True`, `separators=(",", ":")`):

```json
{"adoption_request_id":"<uuid>","expires_at":"<canonical>","hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","hpke_public_key_fingerprint":"<64 hex>","installation_uuid":"<uuid>","mode":"<adoption|reactivation>","protocol":"tablet-adoption-v1","tablet_id":"<uuid>"}
```

Context keys and their exact rules:

| Key | Rule |
| --- | --- |
| `adoption_request_id` | lowercase UUID string |
| `expires_at` | canonical protocol datetime (below) |
| `hpke_ciphersuite` | the exact suite string above |
| `hpke_public_key_fingerprint` | 64 lowercase hex characters |
| `installation_uuid` | lowercase UUID string |
| `mode` | `"adoption"` or `"reactivation"` |
| `protocol` | `"tablet-adoption-v1"` |
| `tablet_id` | lowercase UUID string |

Encoding is ASCII (`json.dumps(...).encode("ascii")`).

## Canonical `expires_at`

`canonical_protocol_datetime(value)`:

- normalizes the aware datetime to UTC (`astimezone(UTC)`),
- serializes with `isoformat()`, then replaces `+00:00` with `Z`,
- **never** emits `+00:00`,
- preserves microseconds when present and omits a zero fractional part
  (ISO-8601 `isoformat` behavior), and
- is deterministic for equivalent instants.

Examples:

    2026-08-14T15:00:00Z
    2026-08-14T15:00:00.000001Z
    2026-08-14T15:00:00.120000Z
    2026-08-14T15:00:00.999999Z

The HTTP preview response `expires_at` string is the **exact same bytes** as the
`expires_at` bound into `info` (both use `canonical_protocol_datetime` on the same
`request.expires_at`). A client that uses the response value verbatim cannot
produce a byte-different `info`.

## HMAC proof

The proof is `HMAC-SHA256(key=info bytes, message=32-byte nonce)` — i.e. the HMAC
key is the exact `info` bytes and the message is the recovered 32-byte nonce. The
server stores only the digest; the nonce is recovered by HPKE opening the
encrypted challenge.

## Client verification order

1. Derive `SHA-256(public key)` and compare against `hpke_public_key_fingerprint`.
2. Reconstruct the canonical context JSON from the response fields (plus the
   known request inputs) and compare `SHA-256(info)` against nothing locally —
   the server stores `canonical_context_hash`; the diagnostic command exposes it.
3. HPKE Base-mode open `enc || ct` to recover the 32-byte nonce.
4. Compute `HMAC-SHA256(info, nonce)` and send it as `challenge_response`.

Failure isolation: fingerprint mismatch → wrong client keypair; context hash
mismatch → canonicalization/API contract mismatch; context matches but open
fails → HPKE mode/suite/framing mismatch; open succeeds but HMAC differs →
proof-construction mismatch.
