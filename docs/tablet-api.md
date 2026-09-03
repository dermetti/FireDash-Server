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
`+00:00` represent UTC. Adoption `expires_at` is specifically
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
| `POST /api/v1/tablet/check-in` | Bearer | No body; optional version/build headers | 200 lease JSON | 403, 426 | ACTIVE operational; eligible current STALE recovers automatically; INACTIVE records control-plane contact without operational renewal |
| `POST /api/v1/tablet/refresh` | Bearer | No body; optional version/build headers | 200 lease JSON | 403, 426 | ACTIVE, unexpired, operational |
| `GET /api/v1/tablet/status` | Bearer | None | 200 status JSON | 403 | Any recognized credential, including REPLACED |
| `GET /api/v1/tablet/configuration` | Bearer | None | 200 configuration JSON | 403 | ACTIVE or INACTIVE current installation with a valid assignment |
| `GET /api/v1/tablet/signing-keys/{version}` | Bearer | None | 200 public key JSON | 403, 404, 426 | ACTIVE or INACTIVE current installation; exact configured public key version |
| `GET /api/v1/tablet/manifest` | Bearer | No body; `If-None-Match` optional | 200 manifest | 202, 304, 403 | ACTIVE returns assigned publications; INACTIVE returns a signed empty dataset list |
| `GET /api/v1/tablet/datasets/{publication_id}/download` | Bearer | No body; `If-None-Match` optional | 200 encrypted bytes | 304, 403, 404 | ACTIVE, unexpired, authorised and manifest-listed |

304 has no body. A 202 manifest is a problem object with `Retry-After: 5`.
Request the complete encrypted artifact: do not send `Range` as part of the
FireDash application protocol, and never accept a partial/206 response as a
fully verified artifact. Protected Nginx may serve ranges, but FireDash
verification requires the complete body.

## App version, build, and compatibility

FireDash app versions are exactly numeric `MAJOR.MINOR.PATCH`; compare the
three components numerically. `app_build` is a positive binary diagnostic
number and is never a compatibility axis. Adoption preview
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

New adoption replaces prior ACTIVE/STALE installations for the tablet. A STALE
installation normally retains its durable credential and recovers through an
eligible check-in; it does not need an administrator invitation. A REPLACED
credential is accepted only by the
terminal status probe and no operational endpoint.

| State | Bearer authenticates | Status | Check-in / Refresh | Config / signing keys / manifest / download | Purge |
| --- | --- | --- | --- | --- | --- |
| ACTIVE and unexpired | Yes | 200 | 200 | Allowed when tablet, vehicle, department, features are authorised | No |
| ACTIVE but expired | Yes until stale transition | 200 with deadline | Check-in may mark stale then recover atomically if eligible; Refresh is 403 | 403 until recovery | Offline lease expired |
| STALE | Yes | 200 | Eligible check-in restores ACTIVE and lease; Refresh is 403 | 403 until recovery | No new data |
| INACTIVE | Yes | 200 | Check-in records control-plane contact only; Refresh is 403 | Configuration/signing key/normal signed empty manifest allowed; dataset download is 403 | No new purge directive |
| REVOKED | Yes | 200, `purge_provisioned_data: true` | 403 | 403 | Purge all provisioned material |
| REPLACED | Status only | 200, `purge_provisioned_data: true` | 403 | 403 | Purge all provisioned material |

Lost, retired, or otherwise unauthorised tablets cannot check in, refresh,
configure, obtain a manifest, or download. An inactive tablet may be adopted
or re-provisioned while it has a valid assignment, but its asset state remains
inactive until an administrator explicitly activates it.

`GET /api/v1/tablet/status` is the safe state probe for any recognized
credential, including a terminal REPLACED credential:

```json
{"status":"active","authorization_valid_until":"2026-08-22T00:00:00Z","purge_provisioned_data":false,"server_time":"2026-08-15T00:00:00Z"}
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

Retain the private P-256 key for the installation lifetime. The corresponding
private key is required to open adoption challenges and later grants.
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

## Lease, check-in, Refresh tablet

Department lease policy is server-owned: default seven days, minimum three.

`POST /api/v1/tablet/check-in` has no body. It always records activity. With more
than 48 hours remaining it leaves `authorization_valid_until` unchanged. With
48 hours or less it sets the deadline to `server_time + department lease`. A
current STALE installation with a valid durable credential automatically becomes
ACTIVE and receives a fresh department lease when the physical Tablet is ACTIVE,
its department and assignment are valid, and no terminal installation state
applies. It never recovers REVOKED or REPLACED credentials.

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

**Historical signing-key retrieval:** the endpoint returns the exact configured
public key for every retained version; unknown versions
return 404. Cache each strictly validated key by version and never substitute
the active key for a different requested version. The server retains historical
public keys while signed material referring to them remains available.

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

### Phonebook datasets

Phonebook delivery is additive JSON dataset delivery using the same encrypted artifact,
signature, and HPKE key-grant contract shown above. A manifest for an assigned Tablet can
contain both `department_phonebook` (department scope) and `station_phonebook` (only the
currently assigned Station scope). It never contains another Station's Phonebook.

Each decrypted JSON artifact has this stable shape:

```json
{"entries":[{"id":"<canonical UUID>","first_name":"Ada","last_name":"Lovelace","organization_unit":"Operations","function":"Duty officer","phone_number":"040 42851 2300"}],"source_revision":1}
```

`department_phonebook` contains only department-wide entries; `station_phonebook` contains
only entries owned by that exact Station. Clients present these two complete datasets together
as one logical Phonebook. They must not assume either dataset includes the other scope.

### Dangerous goods (`dangerous_goods`, `artifact_format: json`)

`dangerous_goods` is a required, department-scoped schema-1 dataset. Its manifest
entry uses `scope:"department"`, `schema_version:1`, `required:true`, and
`artifact_format:"json"`; the HPKE scope binding has the owning `department_id`
and `station_id:null`. It uses the normal encrypted artifact and generic download
URL shown above—there is no dangerous-goods-specific Tablet endpoint.

The decrypted plaintext is the exact UTF-8 JSON byte sequence validated and
retained from the curated source file. It is not reserialized, normalized, or
compressed by FireDash. A representative compact document is:

```json
{
  "dataset_type":"dangerous_goods",
  "schema_version":1,
  "metadata":{"publication_profile":"compact","record_count":1,"eri_card_count":1,"default_name_language":"de","placard_catalog":{"scheme":"adr","delivery":"bundled_with_tablet_app","available_assets":{"3":"...","7A":"...","7B":"...","7C":"..."},"special_values":{"7X":{"kind":"variable","candidate_codes":["7A","7B","7C"],"selection_basis":"transport_index_and_dose_rate"}}}},
  "goods":[{"id":"bam-example-1","un_number":"1203","names":{"official":{"de":"BENZIN","en":"GASOLINE","fr":"ESSENCE"},"aliases":{"de":["MOTORBENZIN"]}},"adr":{"hazard_identification_number":"33","class":"3","classification_code":"F1","packing_group":"II","placards":["3",{"kind":"conditional","code":"6.1"}]},"eri":["3-01"]}],
  "eri_defaults":{"1203":"3-01"},
  "eri_cards":{"3-01":[["title","BENZIN"],["heading","Gefahr"],["item","Geeignete Maßnahmen treffen."]]},
  "sources":[{"id":"bam","provider":"BAM","dataset":"ADR","source_file":"...","sha256":"...","source_url":"...","legal":{"legal_url":"...","license":{},"attribution":{},"processing":{}}},{"id":"ericards","provider":"Cefic","dataset":"ERI","source_file":"...","sha256":"...","source_url":"...","legal":{"terms_url":"...","guidance_url":"...","disclaimer_url":"...","attribution":{},"reproduction":{}}}]
}
```

`metadata.publication_profile` is `"compact"`. `record_count` and
`eri_card_count` describe the complete `goods` list and `eri_cards` map.
Metadata/source provenance is supplied for interpretation and audit; a client
uses the document's `dataset_type` and `schema_version` to select this parser.

Each good has a stable source `id`, a four-digit string `un_number`, and
`names.official`, a non-empty language-keyed map of source strings.
`names.aliases`, when present, is a language-keyed map of string arrays. Language
keys are not limited to `de`, `en`, or `fr`; clients must preserve all supplied
keys and source spelling/content. Search normalization is a client-side concern
and must not mutate stored or displayed source text.

`adr` may contain string fields `hazard_identification_number` (Gefahrnummer),
`class`, `classification_code`, and `packing_group`, plus `placards`. `eri` is
either absent/null/empty or a list of keys in `eri_cards`. `eri_defaults` maps a
four-digit UN number to an ERI card key and is the fallback when the relevant
Gefahrnummer/ERI selection is unavailable. Each `eri_cards` value is an ordered
list of `[kind, text]` pairs: the first pair is `['title', text]`; remaining
kinds are only `title`, `heading`, or `item`. Preserve both order and text.

Placards are semantic data, not server-hosted artwork. An ordinary fixed
placard is a string code such as `"3"` or `"6.1"`. A conditional placard is
`{"kind":"conditional","code":"6.1"}`. The variable `7X` form is exactly
`{"kind":"variable","candidate_codes":["7A","7B","7C"],"selection_basis":"transport_index_and_dose_rate"}`:
select only among those candidates and never interpret or infer `7D`. A
`{"kind":"none"}` placard specifies no placard. A
`{"kind":"reference","reference":"ADR 5.2.2.1.12"}` placard carries ADR
reference text rather than an invented label. Placard SVG assets are
Tablet-bundled presentation resources; catalog asset filenames are not a
required runtime server contract.

Because this entry is `required:true`, a client that cannot understand
`dangerous_goods` schema version 1 must reject the candidate manifest and must
not activate it.

### Manifest Ed25519 verification

Fetch the raw 32-byte public key named by `signing_key_version`. The endpoint
returns that exact configured public-ring entry whether or not it is the active
signing version; an unknown version is 404. Cache keys by exact version and
never substitute the active/current key for a requested historical version.
Remove only
the top-level `signature`. Serialize all remaining members, including
`signature_algorithm`, `signing_key_version`, configuration, datasets, and key
grants using sorted keys, compact separators, and ASCII encoding:

```text
json.dumps(unsigned_manifest, sort_keys=True, separators=(',', ':')).encode('ascii')
```

Strictly Base64-decode the signature and verify Ed25519. Failure makes every
manifest-derived field and new download untrusted: discard the candidate,
retain prior verified state, and report recovery required. The exact helper is
`canonical_manifest_payload` in `apps/publications/manifests.py`. The complete
test-only frozen vector is
`apps/publications/tests/fixtures/complete_manifest_contract.json`; its test
also freezes the distinct quoted response ETag rule (response payload excluding
only `generated_at`). Do not treat the ETag hash as the signed-payload hash.

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
| Stale / automatic recovery | Deadline expired or server says STALE | Stop offline use; retry normal check-in when connected. On success, resume only after normal configuration/manifest verification. |
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
| 401 or 403 | The API does not promise a stable authentication/authorisation distinction. If status can be fetched, use it to distinguish terminal revoked/replaced purge from an eligible stale check-in retry; otherwise enter credential recovery and do not assume cached authority remains valid. |
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
  shape before Base64 encoding and retain the private key for the installation lifetime.
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
`required:true`, schema version 1, with no minimum app version.
`department_klgv_plans` is a registered future-style department dataset with
`required:false`, schema version 1, and `artifact_format:"zip"`; it is
server-feature-disabled and not yet an iOS rendering requirement. It
demonstrates the deliberately additive v1 rule: an older client verifies the
complete manifest, ignores that unsupported optional entry, and continues with
all supported required entries. The internal test-only dataset is not a
production client contract.

### `department_hydrants` (`artifact_format: geojson`)

The plaintext is a GeoJSON FeatureCollection:

```json
{"type":"FeatureCollection","features":[{"type":"Feature","id":"hydrant UUID","geometry":{"type":"Point","coordinates":[longitude,latitude]},"properties":{"external_identifier":"string","street":"string or null","house_number":"string or null","location":"string or null","hydrant_type":"string","diameter_mm":100,"status":"string"}}],"schema_version":1,"source_revision":42}
```

Coordinates are `[longitude, latitude]`. `source_revision` is diagnostic
source tracking, not the publication version.

### `department_fire_plans` (`artifact_format: zip`)

The ZIP contains `manifest.json` and one sanitized PDF per plan at exactly
`plans/{uuid}.pdf`. The manifest is:

```json
{
  "source_revision": 42,
  "fire_plans": [
    {
      "id": "12345678-1234-1234-1234-123456789abc",
      "external_identifier": null,
      "object_name": "Das Rauhe Haus",
      "address": "Am Stadtrand 56 und 56 a",
      "postal_code": "22047",
      "city": "Hamburg",
      "fsd_location": "Lage: FSD befindet sich links vom Eingang Haus 56 (Säule)",
      "bmz_location": "Brandmeldezentrale Haus 56, 1. Obergeschoss",
      "rwa_info": "Auslösung der RWA-Anlage durch Handtaster",
      "longitude": 10.09873774,
      "latitude": 53.59229519,
      "sha256": "<64 lowercase hex>",
      "page_count": 12,
      "path": "plans/12345678-1234-1234-1234-123456789abc.pdf"
    }
  ]
}
```

This metadata lives inside the decrypted `department_fire_plans` dataset
artifact, not in the top-level tablet dataset manifest. Each entry carries the
operational Fire Plan metadata from the canonical record so the tablet can
display, search, and place a plan without inspecting its PDF:

- `external_identifier` is **nullable**: it is `null` when the canonical Fire
  Plan has no external identifier (its address is then the identity). It is
  never serialized as an invented value.
- `object_name` is optional/nullable; it reflects the canonical `object_name`.
- `address`, `postal_code`, and `city` reflect the canonical Fire Plan metadata
  and are `null` when the canonical value is blank.
- `fsd_location`, `bmz_location`, and `rwa_info` are optional operational text
  from the canonical Fire Plan and are `null` when absent. They are bundle-entry
  metadata, not properties of the top-level signed dataset manifest.
- `longitude` and `latitude` are nullable numeric WGS84 (EPSG:4326) coordinates;
  `longitude` (Point.x) comes before `latitude` (Point.y) in the schema, and
  missing coordinates are represented as JSON `null` (never `0`, `0.0`, `""`,
  or `"0,0"`).
- The PDF file continues to be referenced through `path`, exactly
  `plans/{id}.pdf`.
- `sha256` continues to cover the referenced sanitized PDF.

Require each declared path to be exactly its ID-derived `plans/{uuid}.pdf`,
validate every PDF SHA-256 and page count as appropriate to the client parser,
and reject ZIP traversal, duplicate entries, undeclared files, or decompression
sizes that violate local safety limits. Never trust a ZIP member path to choose
an output path.

### `department_fire_plans` (`artifact_format: document-manifest-v2`)

This is the authoritative Fire Plan v2 contract. It is selected only when the
normal signed tablet manifest contains this complete dataset descriptor:

```json
{"publication_id":"11111111-1111-1111-1111-111111111111","type":"department_fire_plans","scope":"department","version":42,"schema_version":2,"required":true,"minimum_app_version":null,"artifact_format":"document-manifest-v2","manifest_url":"/api/v1/tablet/fire-plan-generations/11111111-1111-1111-1111-111111111111/manifest"}
```

The descriptor is emitted only for the authoritative current publication.
Route generically by `type`, `scope`, `schema_version`, and `artifact_format`;
`department_klgv_plans` is also a schema-2 `document-manifest-v2` dataset.
Its discovery descriptor has the same fields as Fire Plans but uses
`/api/v1/tablet/document-generations/{publication_id}/manifest`. Its complete
manifest uses `format: "document-generation-v2"`, `dataset_type:
"department_klgv_plans"`, and each document has a `klgv_plan` object containing
the frozen canonical KLGV metadata. Artifact, grant, signature, HPKE, AES-KW,
AES-GCM/no-AAD, hash, and authorization rules are identical to the Fire Plan
contract; only the metadata adapter and endpoint prefix differ.

### Future PDF-backed datasets

The generic document-generation service owns immutable artifact encryption,
membership references, generation keys, HPKE grants, manifest signing,
artifact authorization, and reference-aware cleanup. A new adapter must
register its dataset type/scope and schema 2 support, supply stable canonical
document UUIDs, frozen metadata, accepted sanitized PDF bytes/SHA-256, complete
membership validation, manifest metadata key, and authorization scope. New PDF
datasets should start on `document-manifest-v2` unless real historical clients
require a legacy format. This is an extension contract, not a claim that any
other dataset has a wire protocol.

All v2 requests require the normal `Authorization: Bearer <installation
credential>` header:

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/v1/tablet/fire-plan-generations/{publication_id}/manifest` | Complete signed manifest plus this installation's generation-key grant. |
| `GET` | `/api/v1/tablet/fire-plan-generations/{publication_id}/artifacts/{artifact_id}/download` | Referenced immutable ciphertext only. |
| `GET` | `/api/v1/tablet/signing-keys/{version}` | `{"algorithm":"Ed25519","version":"1","public_key":"<base64 32-byte key>"}`. |

The manifest response is:

```json
{"format":"fire-plan-generation-v2","publication_id":"11111111-1111-1111-1111-111111111111","version":42,"schema_version":2,"documents":[{"fire_plan":{"id":"22222222-2222-2222-2222-222222222222","external_identifier":"FP-17","object_name":"Example","address":"Example Street 7","postal_code":"12345","city":"Exampletown","fsd_location":null,"bmz_location":null,"rwa_info":null,"longitude":8.123,"latitude":49.456,"sha256":"<64 lowercase hex>","page_count":2,"path":"plans/22222222-2222-2222-2222-222222222222.pdf"},"artifact_id":"33333333-3333-3333-3333-333333333333","sanitized_pdf_sha256":"<64 lowercase hex>","ciphertext_sha256":"<64 lowercase hex>","ciphertext_size":123456,"nonce":"<base64 12 bytes>","encryption_algorithm":"AES-256-GCM","wrapping_algorithm":"AES-KW-RFC3394","kek_version":"1","signature":"<base64 artifact signature>","signature_algorithm":"Ed25519","signing_key_version":"1","generation_wrapped_cek":"<base64 AES-KW wrapped CEK>","generation_key_wrapping_algorithm":"AES-KW-RFC3394","download_path":"/api/v1/tablet/fire-plan-generations/11111111-1111-1111-1111-111111111111/artifacts/33333333-3333-3333-3333-333333333333/download"}],"signature":"<base64 manifest signature>","signature_algorithm":"Ed25519","signing_key_version":"1","generation_key_grant":{"scheme":"HPKE","ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","encapsulated_key":"<base64 65-byte enc>","wrapped_generation_key":"<base64 HPKE ciphertext>","info":{"protocol":"firedash-fire-plan-generation-hpke-v2","publication_id":"11111111-1111-1111-1111-111111111111","installation_id":"44444444-4444-4444-4444-444444444444","tablet_id":"55555555-5555-5555-5555-555555555555","scope":{"dataset_type_code":"department_fire_plans","department_id":"66666666-6666-6666-6666-666666666666","station_id":null},"version_number":42,"schema_version":2}}}
```

`documents` is complete, not a delta, and ordered by canonical Fire Plan UUID.
`fire_plan.id` is canonical identity; `artifact_id` is immutable identity.
`fire_plan.path` is frozen legacy metadata only, never a download URL or local
identity. SHA-256 values are lowercase hex; UUIDs are canonical strings;
binary values use padded Base64; numeric sizes and versions are JSON numbers.

Verify the manifest Ed25519 signature before using any entry. Sign/verify
ASCII `json.dumps(payload, sort_keys=True, separators=(",", ":"))` bytes for
the top-level object before response-only `signature`, `signature_algorithm`,
`signing_key_version`, and `generation_key_grant` are added. The manifest
signs the document artifact metadata including `generation_wrapped_cek`.

HPKE uses RFC 9180 `DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM`.
`generation_key_grant.info` is the complete public binding: validate it against
the signed manifest and local authenticated installation, then ASCII
sorted-key/compact-JSON serialize it as the HPKE `info` bytes. Its explicit
domain separator is `firedash-fire-plan-generation-hpke-v2`. Open the grant to
obtain a 32-byte generation key. AES Key Wrap RFC 3394 unwraps the Base64
`generation_wrapped_cek` to the 32-byte artifact CEK.

Verify ciphertext size and SHA-256 before decryption. Decrypt with AES-256-GCM
using the decoded 12-byte `nonce` and **no AAD** (`nil`/`None`); then require
the plaintext PDF SHA-256 to equal `sanitized_pdf_sha256`. The GCM tag is
appended to ciphertext, not separately encoded.

The document artifact's independent Ed25519 `signature` is **server-side
immutable-artifact integrity/audit metadata**. It was created over UTF-8,
sorted-key compact JSON bytes of exactly:

```json
{"artifact_id":"33333333-3333-3333-3333-333333333333","ciphertext_sha256":"<64 lowercase hex>","ciphertext_size":123456,"encryption_algorithm":"AES-256-GCM","fire_plan_id":"22222222-2222-2222-2222-222222222222","kek_version":"1","nonce":"<base64 12 bytes>","sanitized_pdf_sha256":"<64 lowercase hex>","wrapped_cek":"<base64 server KEK-wrapped CEK>","wrapping_algorithm":"AES-KW-RFC3394"}
```

This signature uses the artifact's server KEK-wrapped CEK (`wrapped_cek`), not
the generation-wrapped CEK. `wrapped_cek` is deliberately not exposed to the
Tablet, so the Tablet **must not attempt to verify this artifact signature**.
The exposed `signature`, `signature_algorithm`, and `signing_key_version`
fields are signed manifest metadata retained for compatibility/diagnostics;
they are not part of the Tablet acceptance chain.

The complete Tablet v2 acceptance chain is: verify the complete manifest
Ed25519 signature; validate its artifact ID, ciphertext hash/size, nonce,
algorithms, sanitized PDF hash, and `generation_wrapped_cek`; verify downloaded
ciphertext size/SHA-256; HPKE-open the generation key; AES-KW unwrap the CEK;
AES-256-GCM decrypt with no AAD; then verify the plaintext sanitized-PDF
SHA-256. Every mandatory step uses wire-visible data plus the installation
private key.

Manifest `200` requires a READY generation-key grant. `202 Accepted` with
`Retry-After: 5` means grant preparation is pending. Unknown generations are
`404`; authorization, revoked/replaced/expired or cross-scope installations,
and missing/non-ready grants are Tablet problem responses (`403`); an
incompatible app can receive `426`. Artifact `200` is
`application/octet-stream` with quoted `ETag` equal to ciphertext SHA-256;
matching `If-None-Match` gives `304`. The artifact must be referenced by that
publication and authorized for that installation; unknown/unreferenced IDs are
`404`. No Range/resume contract exists for v2.

For Fire Plan v2 only, the legacy ZIP still created internally by the server is
a lifecycle compatibility detail. The client must not fetch, decrypt, or
interpret it; use only this manifest and individual artifact endpoints. KLGV
has no legacy ZIP. The v1 ZIP reader remains required only when discovery
advertises the legacy `artifact_format:"zip"` entry.

### `department_klgv_plans` (`artifact_format: document-manifest-v2`)

KLGV begins directly on schema 2 and has no ZIP/v1 compatibility contract.
Its descriptor uses the generic document-generation manifest URL. The manifest
has `format:"document-generation-v2"`, `dataset_type:"department_klgv_plans"`,
and `documents` whose canonical metadata object is `klgv_plan` (the existing
`id`, `external_identifier`, `object_name`, address, postal/city, coordinates,
`sha256`, `page_count`, and `path` fields). Apply the document-v2 acceptance
chain above unchanged. This is additive: Fire Plan v1 ZIP behavior is frozen.
Its generation-key grant uses the same canonical JSON HPKE `info` members as
the document contract, with protocol discriminator
`firedash-document-generation-hpke-v2` (Fire Plan retains its frozen
`firedash-fire-plan-generation-hpke-v2` discriminator).

### `station_personnel` (`artifact_format: json`)

The plaintext is:

```json
{"station_id":"station UUID","source_revision":42,"people":[{"id":"person UUID","display_name":"Name","incident_commander_eligible":true,"commander_email":"verified@example.invalid"}]}
```

`commander_email` may be `null`; it is populated only when verified. Require
the plaintext `station_id` to equal the manifest/configuration station scope.

## Swift compatibility / acceptance checklist

Before releasing an iOS implementation, prove it against the adoption proof,
HPKE DatasetKeyGrant, complete SignedManifest, and artifact-signature/AES-GCM
vectors below. Exercise lost completion-response recovery; 202 with a delay of
at least `Retry-After`; manifest and artifact 304; exact historical signing-key
lookup; same-origin download enforcement; lease expiry; and REVOKED/REPLACED
purge. A candidate manifest with an unsupported `required:false` dataset must
activate its supported required entries; an unsupported required type, schema,
or minimum app version must block activation.

## Current known limitations / interoperability gaps

- **Trust-bootstrap limitation:** authenticated HTTPS to FireDash supplies the
  initial public-key trust. There is no independent signing root or public-key
  pinning mechanism.

## Frozen vectors and contract sources

Use these deterministic materials when building Swift interoperability tests:

| Protocol area | Implementation source | Tests / fixture |
| --- | --- | --- |
| Adoption canonical info and proof | `apps/tablets/services.py`, `apps/publications/hpke.py` | `apps/tablets/tests/test_provisioning_crypto.py`, especially `test_adoption_context_has_a_frozen_mode_bound_canonical_encoding` |
| HPKE grant info and RFC 9180 vector | `apps/publications/hpke.py` | `apps/publications/tests/fixtures/hpke_contract.json`, `apps/publications/tests/test_hpke.py` |
| Complete manifest signed bytes, signature, and ETag | `apps/publications/manifests.py`, `apps/publications/worker_grants.py` | `apps/publications/tests/fixtures/complete_manifest_contract.json`, `apps/publications/tests/test_manifest_contract.py` |
| Artifact signature and AES-GCM metadata | `apps/publications/artifacts.py` | `apps/publications/tests/fixtures/artifact_signature_contract.json`, `apps/publications/tests/test_artifacts.py` |
| Tablet routes, request validation, headers and states | `apps/tablets/api_urls.py`, `apps/tablets/api.py`, `apps/tablets/services.py`, `apps/tablets/models.py` | `apps/tablets/tests/test_api.py`, `apps/tablets/tests/test_adoption_api_crypto.py` |
| Dataset schemas | `apps/publications/registry.py`, `apps/publications/builders.py` | publication builder tests |

## /api/v1 beta freeze

`/api/v1` is **BETA FROZEN**. Existing v1 routes, fields, field types, and
meanings will not be removed, renamed, or changed. Later v1 evolution is
additive and optional; a breaking wire or lifecycle change requires a future
`/api/v2`. Exact-version historical signing-key retrieval is part of this v1
compatibility contract: clients may cache a retained key by version, but must
never substitute another active or historical key.
