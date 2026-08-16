# FireDash Tablet API integration contract

This document is the authoritative iOS/tablet protocol contract. The generated
OpenAPI schema at `/api/v1/schema/` and Swagger UI at `/api/v1/docs/` describe
ordinary object shapes; this document defines lifecycle, retry behavior,
headers, canonical bytes, and cryptographic verification.

## API conventions

All application API paths begin with `/api/v1/` and have **no trailing slash**.
The exceptions are the generated `/api/v1/schema/` and `/api/v1/docs/` paths,
which do have trailing slashes. Production uses HTTPS. Send JSON as
`Content-Type: application/json`. JSON
success uses `application/json`; errors use `application/problem+json`; a
successful artifact transfer uses `application/octet-stream`.

Use lower-case, hyphenated UUID strings. Binary fields use strict standard RFC
4648 Base64 (`+` and `/`, with padding): reject URL-safe substitutions,
whitespace, malformed padding, PEM, DER, and JWK wrappers. The opaque
installation credential is URL-safe and must be used verbatim.

Timestamps are ISO 8601/RFC 3339 strings. In normal API data, both `Z` and
`+00:00` represent UTC. Adoption/reactivation `expires_at` is specifically
canonical UTC with `Z`, is byte-bound into HPKE `info`, and must be preserved
as the exact returned string. Manifest timestamp strings are signed and must
not be normalized before verification.

Authenticated calls use exactly:

```http
Authorization: Bearer <opaque-installation-credential>
```

Never send an installation ID as an authentication selector. The server finds
the credential itself and compares its protected digest.

Errors have this RFC 9457 shape:

```json
{"type":"https://fire-backend.internal/problems/<code>","title":"Forbidden","status":403,"code":"stable_machine_code","detail":"Safe reason.","request_id":"correlation id"}
```

Switch on HTTP status plus `code`, never title or detail. Keep `request_id` only for support diagnostics. Current authentication and
authorisation failures are rendered as 403; malformed input is 400; unknown
routes/resources are 404; a queued manifest is 202; conditional matches are
304. No tablet 409 or rate-limit/429 contract currently exists.

## Endpoint matrix

| Method and path | Authentication | Request body / headers | Success | Other results | Eligible state |
| --- | --- | --- | --- | --- | --- |
| `POST /api/v1/adoption/preview` | None | preview JSON | 201 challenge | 400, 403 | Valid unused adoption invitation; operational tablet |
| `POST /api/v1/adoption/complete` | None | completion JSON | 201 credential | 400, 403 | Matching unexpired challenge/invitation |
| `POST /api/v1/tablet/reactivation/preview` | None | preview JSON | 201 challenge | 400, 403 | Valid invitation for STALE installation |
| `POST /api/v1/tablet/reactivation/complete` | Existing Bearer on first completion; exact proof-only recovery replay for 10 minutes | completion JSON | 201 rotated credential | 400, 403 | Same STALE installation/request |
| `POST /api/v1/tablet/check-in` | Bearer | No body; optional version/build headers | 200 lease JSON | 403, 426 | ACTIVE, unexpired, operational |
| `POST /api/v1/tablet/refresh` | Bearer | No body; optional version/build headers | 200 lease JSON | 403, 426 | ACTIVE, unexpired, operational |
| `GET /api/v1/tablet/status` | Bearer | None | 200 status JSON | 403 | Any recognized credential, including REPLACED |
| `GET /api/v1/tablet/configuration` | Bearer | None | 200 configuration JSON | 403 | ACTIVE, unexpired, authorised |
| `GET /api/v1/tablet/signing-keys/{version}` | Bearer | None | 200 public key JSON | 403, 404, 426 | Current authorised installation; active key only |
| `GET /api/v1/tablet/manifest` | Bearer | No body; `If-None-Match` optional | 200 manifest | 202, 304, 403 | ACTIVE, unexpired, authorised |
| `GET /api/v1/tablet/datasets/{publication_id}/download` | Bearer | No body; `If-None-Match` optional | 200 encrypted bytes | 304, 403, 404 | ACTIVE, unexpired, authorised and manifest-listed |

304 has no body. A 202 manifest is a problem object with `Retry-After: 5`.
Request the complete encrypted artifact: do not send `Range` as part of the
FireDash application protocol, and never accept a partial/206 response as a
fully verified artifact. Protected Nginx may serve ranges, but FireDash
verification requires the complete body.

## App version, build, and compatibility

FireDash app versions are exactly numeric `MAJOR.MINOR.PATCH`; compare the
three components numerically. `app_build` is a positive binary diagnostic
number and is never a compatibility axis. Adoption and reactivation preview
require `app_version` and accept optional `app_build`. Check-in and Refresh
may send:

```http
X-FireDash-App-Version: 1.2.0
X-FireDash-App-Build: 57
```

With neither header, stored telemetry is unchanged. A valid version/build pair
replaces both. Version without build preserves the existing build only if the
version is unchanged; otherwise it clears the unknown build. Build without a
version, or malformed telemetry, is ignored without changing authorization.

Every `/api/v1/...` handler selects API major 1 from its server route. System
administrators may configure a database-backed minimum app version for that
major; no policy row or blank value is unrestricted. If configured and the
effective current version is lower, non-status calls return 426 with
`code: "client_update_required"` and `minimum_app_version`. Check-in and
Refresh process valid telemetry before this decision, then do not renew or top
up an unsupported client. Dataset schema/minimum version and immutable
publication version remain independent compatibility concepts.

## Provisioning origin and completion recovery

The administrator QR code contains compact JSON with exactly
`"protocol":"firedash-provisioning-v1"`, HTTPS `origin`, and `token`.
Persist the origin and resolve API paths below `<origin>/api/v1/`; manual entry
uses the same origin/token pair. Resolve relative downloads only against that
same origin and never forward bearer credentials cross-origin.

For a lost successful completion response, the exact completed request and
cryptographic proof can be retried for a fixed 10-minute window. Each valid
retry rotates a fresh credential for the same installation, never creates a
second installation, and never extends the window. This recovery remains valid
despite the invitation being consumed by the original successful request, but
fails for wrong proof, mode, binding, expiry, or administrative invalidation.

## Credential lifecycle and installation states

Generate one random UUID as `installation_uuid` and one P-256 key pair before
adoption. Completion returns `credential` exactly once. Store it in Keychain
with device-only access appropriate to the application; never log, display,
back up, or place it in a URL.

New adoption replaces prior ACTIVE/STALE installations for the tablet.
Reactivation retains the installation but rotates its credential; atomically
replace the old Keychain value. A REPLACED credential is accepted only by the
terminal status probe and no operational endpoint.

| State | Bearer authenticates | Status | Check-in / Refresh | Config / manifest / download | Reactivation | Purge |
| --- | --- | --- | --- | --- | --- | --- |
| ACTIVE and unexpired | Yes | 200 | 200 | Allowed when tablet, vehicle, department, features are authorised | No | No |
| ACTIVE but expired | Yes until stale transition | 200 with deadline | 403; check-in marks stale | 403 | Requires stale invitation | Offline lease expired |
| STALE | Yes | 200 | 403 | 403 | Preview with invitation; completion with same Bearer | No new data |
| REVOKED | Yes | 200, `purge_provisioned_data: true` | 403 | 403 | No | Purge all provisioned material |
| REPLACED | Status only | 200, `purge_provisioned_data: true` | 403 | 403 | No | Purge all provisioned material |

Removed, lost, retired, inactive, or otherwise unauthorised tablets cannot
adopt, reactivate, check in, refresh, configure, obtain a manifest, or download.

`GET /api/v1/tablet/status` is the safe state probe for any recognized
credential, including a terminal REPLACED credential:

```json
{"status":"active","authorization_valid_until":"2026-08-22T00:00:00Z","purge_provisioned_data":false}
```

REVOKED and REPLACED return `purge_provisioned_data: true`. A required purge removes
the credential, private key, CEKs, verified encrypted artifacts, decrypted or
plaintext cache, and manifest/configuration/cache metadata. Treat a denied
status request as replacement/invalid-credential recovery rather than a reason
to reuse cached authority indefinitely.

## P-256 HPKE requirements

Use NIST P-256/secp256r1. The suite string is exactly:

```text
DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM
```

The public key is an ANSI X9.62 uncompressed point: exactly 65 bytes,
`0x04 || X(32 bytes) || Y(32 bytes)`, then standard Base64. Compressed points,
PEM, DER/SPKI, JWK, another curve, scalar bytes, and any other length are
rejected. Its fingerprint is lower-case hexadecimal
`SHA-256(uncompressed-point-bytes)`.

Retain the private P-256 key for the installation lifetime. Reactivation
requires exactly the existing installation UUID, suite, and public key, which
requires the corresponding private key to open the challenge and later grants.
The protocol does not establish Secure Enclave compatibility; use a key-storage
mechanism only if it can provide this exact P-256/HPKE representation.

## Adoption: byte-exact flow

An administrator supplies an out-of-band adoption token. Invitations normally
last 15 minutes; a preview challenge lasts 5 minutes. The fixed suite is
mandatory: an unsupported `hpke_ciphersuite` is rejected at preview.

`POST /api/v1/adoption/preview` is unauthenticated:

```json
{"token":"invitation token","installation_uuid":"11111111-1111-1111-1111-111111111111","app_version":"1.2.3","hpke_public_key":"Base64(65-byte X9.62 point)","hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM"}
```

It returns 201:

```json
{"adoption_request_id":"22222222-2222-2222-2222-222222222222","encrypted_challenge":"Base64(enc || ciphertext)","expires_at":"2026-08-09T12:39:56.789012Z","tablet_id":"33333333-3333-3333-3333-333333333333","hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","hpke_public_key_fingerprint":"64 lower-case hex characters","mode":"adoption","protocol":"tablet-adoption-v1"}
```

Decode `encrypted_challenge` as `enc || ciphertext`. For P-256, `enc` is the
first 65 bytes; the remainder is the HPKE ciphertext. Open it with the private
key. Plaintext is a 32-byte nonce.

Build HPKE `info` from the response plus the original installation UUID. It is
ASCII, sorted-key, compact JSON; do not normalize or reformat `expires_at`:

```json
{"adoption_request_id":"22222222-2222-2222-2222-222222222222","expires_at":"2026-08-09T12:39:56.789012Z","hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","hpke_public_key_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","installation_uuid":"11111111-1111-1111-1111-111111111111","mode":"adoption","protocol":"tablet-adoption-v1","tablet_id":"33333333-3333-3333-3333-333333333333"}
```

Compute `HMAC-SHA256(key=decrypted_nonce, message=exact_info_ascii_bytes)`.
Then call `POST /api/v1/adoption/complete`:

```json
{"adoption_request_id":"22222222-2222-2222-2222-222222222222","challenge_response":"Base64(32-byte HMAC)","confirmed":true}
```

201 returns the installation ID, one-time credential, initial deadline, and
server-time anchor:

```json
{"installation_id":"44444444-4444-4444-4444-444444444444","credential":"opaque URL-safe credential","authorization_valid_until":"2026-08-16T12:39:56.789012Z","server_time":"2026-08-09T12:39:56.789012Z"}
```

Persist UUID, private-key reference, credential, deadline, and server-time
anchor atomically. If the successful completion response is lost, replay the
**exact same** completion request and proof within the fixed ten-minute
recovery window. Do not create a new preview or alter the proof. Each accepted
recovery replay rotates the unknown credential and returns a fresh one; persist
only that returned credential. The window starts with the first successful
completion and never extends.

`confirmed:false` fails completion without incrementing proof counters. An
invalid HMAC increments both the request and invitation counters; on the fifth
invalid proof the invitation is revoked. Expired or already-completed requests,
used or revoked invitations, changed proof/request/mode bindings, and replay
after the recovery window fail. Successful completion consumes the invitation
for new provisioning, but that consumption does not prevent the exact bounded
lost-response recovery replay. Administrative replay invalidation, invitation
revocation, and tablet removal also block recovery.

## Reactivation: byte-exact flow

An administrator creates an invitation only for a STALE installation.
`POST /api/v1/tablet/reactivation/preview` is unauthenticated but requires that token.
Its request is the adoption preview request with the existing installation UUID,
exact stored public key, and same suite. Its response has
`"mode":"reactivation"` and `"protocol":"tablet-adoption-v1"`.

Construct/decrypt the challenge exactly as above, changing only `mode` in
canonical HPKE info to `reactivation`. Complete with the same completion JSON
at `POST /api/v1/tablet/reactivation/complete` and the existing STALE installation
Bearer credential. The server binds the request to that credential's
installation. Success is the same 201 object as adoption completion, with a
rotated one-time credential and a fresh department lease. ACTIVE, REVOKED,
REPLACED, removed, lost, retired, and inactive tablets cannot use reactivation
as a shortcut. The confirmation, expiry, invalid-proof counter, five-failure
revocation, used/revoked invitation, and replay rules above apply equally to
reactivation requests.

## Lease, check-in, Refresh tablet

Department lease policy is server-owned: default seven days, minimum three.

`POST /api/v1/tablet/check-in` has no body. It always records activity. With more
than 48 hours remaining it leaves `authorization_valid_until` unchanged. With
48 hours or less it sets the deadline to `server_time + department lease`.
It cannot revive an expired/stale installation.

`POST /api/v1/tablet/refresh` is the firefighter's explicit user action and has no
body. For an ACTIVE, unexpired, operational installation it records activity
and sets:

```text
authorization_valid_until = max(current_expiry, server_time + department_lease)
```

Repeated presses do not stack lease periods; a longer deadline is not shortened.
Refresh audits the action but does not create a `SignedManifest`,
`DatasetKeyGrant`, signature, or download. Both endpoints return 200:

```json
{"status":"active","server_time":"2026-08-15T00:00:00Z","authorization_valid_until":"2026-08-22T00:00:00Z"}
```

One iOS **Refresh tablet** action is this orchestration, not a combined API:

1. `POST /api/v1/tablet/refresh`.
2. `GET /api/v1/tablet/configuration`.
3. Conditional `GET /api/v1/tablet/manifest` using a trusted cached ETag.
4. On 202, retain verified cache; wait **at least** the supplied `Retry-After`
   duration and never retry earlier, then retry manifest.
5. On 304, retain verified manifest/datasets.
6. On 200, verify manifest, reconcile changed/missing artifacts, then commit
   the complete verified candidate atomically.

At app start/foreground, use normal check-in, configuration, then conditional
manifest retrieval. Do not make automatic check-in an explicit lease top-up.

## Configuration and signing keys

`GET /api/v1/tablet/configuration` returns:

```json
{"installation_id":"...","tablet_id":"...","department_id":"...","station_id":"...","vehicle_id":"..."}
```

This is the currently authorised vehicle assignment. `department_id` scopes
department datasets; `station_id` scopes station datasets; `vehicle_id` is the
assignment identity. Refresh configuration before processing changed
authorisation state or applying a newly retrieved manifest.

`GET /api/v1/tablet/signing-keys/{version}` returns:

```json
{"algorithm":"Ed25519","version":"1","public_key":"Base64(raw-32-byte-Ed25519-key)"}
```

Require `algorithm == "Ed25519"`, the requested matching version, strict
Base64, and exactly 32 decoded bytes. In the current beta, the initial trust
bootstrap is the key obtained from this authenticated HTTPS FireDash backend.
There is currently no independent signing root and no public-key pinning
mechanism. HTTPS transport and installation authentication are therefore part
of the initial signing-key trust decision.

**Current server limitation — signing-key rotation:** the endpoint exposes only
the configured active key version and returns 404 for old/unknown versions.
Retain trusted keys by version with verified cache while artifacts/manifests
signed by them may remain usable; an old key not retained locally cannot
currently be fetched again.

## Manifest retrieval and complete wire format

`GET /api/v1/tablet/manifest` only reads/coalesces database work in the web process;
it never loads the KEK or signing private key. A ready manifest returns 200 and
a quoted ETag. The ETag is SHA-256 of sorted compact JSON of the response payload
excluding `generated_at`; send it unchanged in `If-None-Match`. A match returns
304 with the same ETag and no body.

If grant/manifest work is pending, return 202:

```json
{"type":"https://fire-backend.internal/problems/manifest-pending","title":"Manifest pending","status":202,"detail":"The authorized manifest is being prepared.","request_id":"...","manifest_request_id":"..."}
```

It has `Content-Type: application/problem+json` and `Retry-After: 5`.
Unchanged concurrent requests coalesce to the same work. Never replace trusted
cache on 202. Persist a new ETag only after manifest verification and all
required dataset updates commit safely.

Representative complete 200 body (names are exact):

```json
{
  "manifest_generation":1,
  "signature_algorithm":"Ed25519",
  "signing_key_version":"1",
  "generated_at":"2026-08-15T00:00:00+00:00",
  "authorization_valid_until":"2026-08-22T00:00:00+00:00",
  "configuration":{"installation_id":"11111111-1111-1111-1111-111111111111","tablet_id":"22222222-2222-2222-2222-222222222222","department_id":"33333333-3333-3333-3333-333333333333","station_id":"44444444-4444-4444-4444-444444444444","vehicle_id":"55555555-5555-5555-5555-555555555555"},
  "datasets":[{
    "publication_id":"66666666-6666-6666-6666-666666666666","type":"station_personnel","scope":"station","version":7,"schema_version":1,"required":true,"minimum_app_version":null,"artifact_format":"json","encrypted_size":1234,"ciphertext_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","content_encryption_algorithm":"AES-256-GCM","content_encryption_nonce":"Base64(12 bytes)","content_key_wrapped_for_kek":"Base64(AES-KW wrapped CEK)","content_key_wrapping_algorithm":"AES-KW-RFC3394","content_key_kek_version":"1","artifact_signature":"Base64(64-byte Ed25519 signature)","artifact_signature_algorithm":"Ed25519","artifact_signing_key_version":"1","download_url":"/api/v1/tablet/datasets/66666666-6666-6666-6666-666666666666/download",
    "key_grant":{"scheme":"HPKE","ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","encapsulated_key":"Base64(65-byte enc)","wrapped_content_key":"Base64(HPKE ciphertext)"}
  }],
  "signature":"Base64(64-byte Ed25519 signature)"
}
```

`version` is an immutable publication-attempt version. Failed/obsolete attempts
consume numbers, so clients must not assume `vN+1` exists.

### Manifest Ed25519 verification

Fetch the raw 32-byte public key named by `signing_key_version`. Remove only
the top-level `signature`. Serialize all remaining members, including
`signature_algorithm`, `signing_key_version`, configuration, datasets, and key
grants using sorted keys, compact separators, and ASCII encoding:

```text
json.dumps(unsigned_manifest, sort_keys=True, separators=(',', ':')).encode('ascii')
```

Strictly Base64-decode the signature and verify Ed25519. Failure makes every
manifest-derived field and new download untrusted: discard the candidate,
retain prior verified state, and report recovery required. The exact helper is
`canonical_manifest_payload` in `apps/publications/manifests.py`, covered by
`apps/publications/tests/test_manifests.py`; no frozen complete-manifest fixture
currently exists.

The verified manifest supplies the publication identity/binding: its signed
dataset entry binds the publication ID, scope, version, artifact metadata,
download URL, and HPKE grant. The artifact signature is a second integrity
binding for the encrypted artifact metadata, but is not by itself a standalone
assertion of a publication identity.

## Client persistence and cache transaction

Persist installation state atomically enough that a crash cannot leave a
credential paired with the wrong private key or a trusted ETag paired with
unverified bytes. The durable model should include:

- installation UUID, Keychain private-key reference, and Bearer credential;
- last verified server-time anchor and `authorization_valid_until`;
- current configuration and trusted signing public keys by version;
- verified manifest, quoted manifest ETag, and manifest generation;
- per-publication manifest metadata, verified ciphertext, and quoted artifact
  ETag;
- optional decrypted representation/cache, separated from ciphertext.

Maintain three distinct states: unverified download (temporary only), verified
encrypted artifact, and decrypted cache. Verify into a temporary directory,
then atomically replace the verified metadata/files only after every required
manifest and changed dataset succeeds. A failed candidate must never overwrite
known-good verified ciphertext or plaintext.

When a configuration/manifest no longer authorises a dataset, remove its CEK
and decrypted representation. On a server `purge_provisioned_data: true`, a
REPLACED credential, or explicit local deprovisioning, remove the credential,
private key, CEKs, verified artifacts, plaintext/decrypted cache, and
manifest/configuration/cache metadata. Encrypted residue without a CEK is not
usable, but prompt complete purge is the required client behavior.

## Client state machine

| State | Meaning | Required action / transition |
| --- | --- | --- |
| Unprovisioned | No usable credential/key pair | Generate UUID/P-256 pair and complete adoption. |
| Adoption preview/challenge | Challenge held only until expiry | Open 32-byte nonce, HMAC exact info, complete once. |
| Active | Credential and lease valid | Foreground: check-in, configuration, conditional manifest. |
| Refreshing | User selected **Refresh tablet** | Refresh, configuration, conditional manifest, reconcile datasets. |
| Manifest pending | Server returned 202 | Retain cache; wait at least `Retry-After` and never retry earlier, then retry only manifest. |
| Manifest ready | Verified 200 or trusted 304 | Use only fully verified data until deadline. |
| Offline-valid | Network unavailable before deadline | Use prior verified data and elapsed server-time anchor. |
| Stale / reactivation required | Deadline expired or server says STALE | Do not attempt check-in/refresh recovery; use stale invitation flow. |
| Revoked / replaced | Server denies use or asks purge | Purge local provisioned material; do not retry as active. |

For offline authorization, anchor the last verified server time to a monotonic
clock reading. Estimate current server time only as `verified_server_time +
monotonic_elapsed`; never move the estimate backwards from wall-clock changes.
At/after `authorization_valid_until`, stop using decrypted/reference data for
operational use, erase cached CEKs and decrypted plaintext, and enter recovery.
Verified encrypted artifacts may remain only as inert recovery/cache material;
they must not be decrypted or presented after lease expiry. Offline mode may
use only already verified data; it must not activate unverified downloads or
infer a lease renewal.

## Errors, retries, and recovery actions

| Result or failure | Client action |
| --- | --- |
| Transport failure / 5xx | Retain verified state; use bounded exponential backoff with jitter. |
| 202 manifest | Retain cache; wait at least the supplied `Retry-After` duration and never retry earlier before manifest retry. This remains correct when iOS resumes later from suspension/backgrounding. |
| 304 | No body: retain the corresponding verified cache and ETag. |
| 400 | Treat as client/protocol error; do not blind-retry malformed canonical crypto input. |
| 401 or 403 | The API does not promise a stable authentication/authorisation distinction. If status can be fetched, use it to choose stale reactivation versus revoked/replaced purge; otherwise enter credential recovery and do not assume cached authority remains valid. |
| 404 | Treat unknown signing key/resource/publication as a safe protocol/state error; do not substitute another key or dataset. |
| 409 / 429 | Not currently a tablet API contract; if received, do not infer special semantics unless a future server contract defines them. |
| Invalid Base64, point, JSON canonicalization, or UUID/scope | Reject the candidate without cache replacement. |
| Manifest or artifact Ed25519 failure | Security failure: discard candidate, retain known-good cache, surface support/recovery. |
| HPKE or AES-GCM failure | Reject candidate and its CEK; do not attempt plaintext parsing. |
| Size/hash/ZIP/schema/app-version failure | Reject candidate; do not activate partial data. |

Honor a server `Retry-After` as a minimum delay: never retry earlier. Other
retries must be bounded; do not spin on a permanently invalid credential, a
malformed payload, or a cryptographic verification failure.

## iOS implementation notes

- Put credentials and the private-key reference in Keychain with the least
  exportable/accessibility setting compatible with operational requirements.
- Use P-256 public bytes in uncompressed X9.62 form. Validate the 65-byte
  shape before Base64 encoding and retain the same key for reactivation.
- Standard Base64, timestamp bytes, field names, sorted keys, separators, and
  ASCII/UTF-8 encodings are protocol data. Generic JSON reserialization that
  changes any of them will break verification.
- HTTPS authenticates transport, not publication content. Never accept a
  manifest/artifact merely because the HTTP request succeeded.
- Do not infer version ordering or expect consecutive published versions.
- Use temporary files plus atomic replacement; never silently continue after a
  cryptographic failure.
- This contract does not endorse a particular Swift HPKE implementation. Prove
  the selected implementation against the frozen vectors before deployment.

## DatasetKeyGrant: HPKE unwrap

For each dataset, decode `key_grant.encapsulated_key` as exactly 65 bytes and
`key_grant.wrapped_content_key` as the HPKE ciphertext. Open it with the
installation private P-256 key and the fixed suite stated above. Its `info`
bytes are ASCII, sorted-key, compact JSON:

```json
{"ciphertext_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","installation_id":"11111111-1111-1111-1111-111111111111","protocol":"firedash-hpke-v1","publication_id":"66666666-6666-6666-6666-666666666666","schema_version":1,"scope":{"dataset_type_code":"station_personnel","department_id":"33333333-3333-3333-3333-333333333333","station_id":"44444444-4444-4444-4444-444444444444"},"tablet_id":"22222222-2222-2222-2222-222222222222","version_number":7}
```

The field set is exact: `ciphertext_sha256`, `installation_id`, `protocol`,
`publication_id`, `schema_version`, `scope`, `tablet_id`, and
`version_number`. `scope` always has `dataset_type_code`, `department_id`, and
`station_id` (the latter is JSON `null` for department scope). Use
`json.dumps(info, sort_keys=True, separators=(',', ':')).encode('ascii')` in
equivalent Swift canonicalization. The resulting CEK is exactly 32 bytes; any
wrong point, Base64, suite, `info`, or authentication failure is fatal for that
candidate dataset. Do not try another publication or retain unverified output.

`content_key_wrapped_for_kek` is opaque, server-side KEK-wrapped, signed
metadata. The iPad has no server KEK and **never** AES-KW unwraps it. Its usable
32-byte CEK comes exclusively from HPKE-opening
`key_grant.wrapped_content_key`. The AES-KW value is still required verbatim
when reconstructing the artifact-signature payload.

`apps/publications/tests/fixtures/hpke_contract.json` is the deterministic
HPKE info and RFC 9180 interoperability vector. Its expected canonical
`info` must match byte-for-byte.

## Artifact download, verification, and decryption

Request the manifest-provided relative `download_url`; it is currently:

```text
GET /api/v1/tablet/datasets/{publication_id}/download
```

Send the verified encrypted-artifact ETag in `If-None-Match`, including its
quotes. A 304 has no body and retains the existing verified ciphertext. A 200
has `Content-Type: application/octet-stream`, a quoted cryptographic `ETag`
equal to `"` + `ciphertext_sha256` + `"`, `Accept-Ranges: bytes`, and the
complete encrypted artifact. The application-level ETag survives the internal
Nginx handoff; it is not an mtime/size ETag. The manifest authorises the
publication ID, so an arbitrary known ID is not downloadable.

On a 200, before accepting bytes:

1. Require byte count to equal `encrypted_size`.
2. Calculate SHA-256 over the downloaded ciphertext and require exact match
   with `ciphertext_sha256` and the unquoted ETag value.
3. Verify the artifact Ed25519 signature below.
4. HPKE-open the key grant and require a 32-byte CEK.
5. AES-256-GCM decrypt with the decoded 12-byte
   `content_encryption_nonce`, CEK, and no AAD (`nil`).
6. Parse and validate the resulting plaintext according to `artifact_format`,
   dataset type, scope, and schema version.

The download is AESGCM ciphertext in which the 16-byte GCM authentication tag
is appended by the AEAD library. It is not separately transmitted. A size,
hash, signature, HPKE, AEAD, ZIP, or schema failure makes the candidate
untrusted: delete its temporary files and retain the last verified cache.

### Artifact Ed25519 signature

The manifest artifact key version identifies the same 32-byte Ed25519 public
key retrieval/validation process. The artifact signature independently binds
the encrypted artifact and its publication scope/version; the manifest
signature binds the dataset list, grants, configuration, and lease.

Verify `artifact_signature` over UTF-8, sorted-key, compact JSON exactly:

```json
{"ciphertext_sha256":"<64-lowercase-hex>","ciphertext_size":1234,"encryption_algorithm":"AES-256-GCM","kek_version":"1","nonce":"Base64(12 bytes)","schema_version":1,"scope":{"dataset_type_code":"station_personnel","department_id":"33333333-3333-3333-3333-333333333333","station_id":"44444444-4444-4444-4444-444444444444"},"version_number":7,"wrapped_cek":"Base64(AES-KW wrapped CEK)","wrapping_algorithm":"AES-KW-RFC3394"}
```

Use `content_key_wrapped_for_kek` for `wrapped_cek`,
`content_key_wrapping_algorithm` for `wrapping_algorithm`,
`content_encryption_algorithm` for `encryption_algorithm`,
`content_encryption_nonce` for `nonce`, and `content_key_kek_version` for
`kek_version`. Those names intentionally differ from the artifact-signature
payload. `publication_id`, manifest signature fields, and HPKE grant are not
part of this payload. Decode/verify `artifact_signature` as a 64-byte Ed25519
signature. The deterministic canonical payload, ciphertext, signature, and
public key are in
`apps/publications/tests/fixtures/artifact_signature_contract.json`, validated
by `test_artifact_signature_contract_fixture_is_canonical_and_verifiable`.

## Dataset plaintext formats

Every entry has a registered `schema_version`. An unsupported **required**
dataset, schema, or `minimum_app_version` blocks activation of the candidate
manifest. An unsupported optional dataset may be ignored only when the server
marks that entry `required:false`; retain existing verified data rather than
activating an unverified replacement. The current production datasets
`department_hydrants`, `department_fire_plans`, and `station_personnel` are all
`required:true`, schema version 1, with no minimum app version. The internal
test-only dataset is not a production client contract.

### `department_hydrants` (`artifact_format: geojson`)

The plaintext is a GeoJSON FeatureCollection:

```json
{"type":"FeatureCollection","features":[{"type":"Feature","id":"hydrant UUID","geometry":{"type":"Point","coordinates":[longitude,latitude]},"properties":{"external_identifier":"string","hydrant_type":"string","diameter_mm":100,"status":"string"}}],"schema_version":1,"source_revision":42}
```

Coordinates are `[longitude, latitude]`. `source_revision` is diagnostic
source tracking, not the publication version.

### `department_fire_plans` (`artifact_format: zip`)

The ZIP contains `manifest.json` and one sanitized PDF per plan at exactly
`plans/{uuid}.pdf`. The manifest is:

```json
{"source_revision":42,"fire_plans":[{"id":"plan UUID","sha256":"64-lowercase-hex","page_count":12,"path":"plans/plan UUID.pdf"}]}
```

Require each declared path to be exactly its ID-derived `plans/{uuid}.pdf`,
validate every PDF SHA-256 and page count as appropriate to the client parser,
and reject ZIP traversal, duplicate entries, undeclared files, or decompression
sizes that violate local safety limits. Never trust a ZIP member path to choose
an output path.

### `station_personnel` (`artifact_format: json`)

The plaintext is:

```json
{"station_id":"station UUID","source_revision":42,"people":[{"id":"person UUID","display_name":"Name","incident_commander_eligible":true,"commander_email":"verified@example.invalid"}]}
```

`commander_email` may be `null`; it is populated only when verified. Require
the plaintext `station_id` to equal the manifest/configuration station scope.

## Current known limitations / interoperability gaps

- **Runtime/server limitation - signing-key rotation:**
  `/api/v1/tablet/signing-keys/{version}` serves only the active configured
  key. The client must retain old already-trusted public keys while signed
  manifests/artifacts using them remain in its cache. There is no historical
  server key ring, independent signing root, or public-key pinning mechanism
  in the current beta.
- **Missing interoperability fixture:** there is no deterministic complete
  signed-manifest fixture. A future fixture should contain the complete
  manifest object, exact unsigned canonical bytes, SHA-256 of those bytes,
  Ed25519 public key, signature, expected quoted ETag, and signing-key version.
  Do not invent those values in a client implementation.
- **Validation gap in this environment:** database-backed tablet/manifest API
  tests may be unavailable when the development PostgreSQL server rejects the
  host in `pg_hba.conf` or the configured role cannot create `test_firedash`.
  That is an environment/test-permission gap, not by itself evidence of a
  protocol defect. Do not weaken database roles or HBA policy to run them.

## Frozen vectors and contract sources

Use these deterministic materials when building Swift interoperability tests:

| Protocol area | Implementation source | Tests / fixture |
| --- | --- | --- |
| Adoption/reactivation canonical info and proof | `apps/tablets/services.py`, `apps/publications/hpke.py` | `apps/tablets/tests/test_provisioning_crypto.py`, especially `test_adoption_context_has_a_frozen_mode_bound_canonical_encoding` |
| HPKE grant info and RFC 9180 vector | `apps/publications/hpke.py` | `apps/publications/tests/fixtures/hpke_contract.json`, `apps/publications/tests/test_hpke.py` |
| Manifest signed bytes and ETag | `apps/publications/manifests.py`, `apps/publications/worker_grants.py` | `apps/publications/tests/test_manifests.py` |
| Artifact signature and AES-GCM metadata | `apps/publications/artifacts.py` | `apps/publications/tests/fixtures/artifact_signature_contract.json`, `apps/publications/tests/test_artifacts.py` |
| Tablet routes, request validation, headers and states | `apps/tablets/api_urls.py`, `apps/tablets/api.py`, `apps/tablets/services.py`, `apps/tablets/models.py` | `apps/tablets/tests/test_api.py`, `apps/tablets/tests/test_adoption_api_crypto.py` |
| Dataset schemas | `apps/publications/registry.py`, `apps/publications/builders.py` | publication builder tests |
