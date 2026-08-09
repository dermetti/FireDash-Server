# FireDash Tablet API: iOS Integration Reference

This document describes the runtime contract implemented by FireDash for a Swift/iOS tablet client. It is derived from the URL routing, DRF views and serializers, tablet lifecycle services, publication worker/manifests, and their tests. It intentionally documents only endpoints rooted at `/api/v1/`.

All examples use synthetic UUIDs, tokens, keys, hashes, and timestamps. Do not use any example credential or key material in an application.

## API Surface

The server generates OpenAPI 3.1.0 (`FireDash Provisioning API`, version `1.0.0`) at `GET /api/v1/schema/`; interactive Swagger UI is at `GET /api/v1/docs/`. The application endpoint paths below have no trailing slash. The schema and docs paths do have trailing slashes.

| Method | Path | Authentication | Success |
| --- | --- | --- | --- |
| `POST` | `/api/v1/adoption/preview` | None; adoption token is in the body | `201` |
| `POST` | `/api/v1/adoption/complete` | None | `201` |
| `POST` | `/api/v1/tablet/reactivation/preview` | None; reactivation token is in the body | `201` |
| `POST` | `/api/v1/tablet/reactivation/complete` | Installation Bearer credential | `201` |
| `POST` | `/api/v1/tablet/check-in` | Installation Bearer credential | `200` |
| `GET` | `/api/v1/tablet/status` | Installation Bearer credential | `200` |
| `GET` | `/api/v1/tablet/configuration` | Installation Bearer credential | `200` |
| `GET` | `/api/v1/tablet/signing-keys/{version}` | Installation Bearer credential | `200` |
| `GET` | `/api/v1/tablet/manifest` | Installation Bearer credential | `200`, `202`, or `304` |
| `GET` | `/api/v1/tablet/datasets/{publication_id}/download` | Installation Bearer credential | `200` or `304` |

Use HTTPS in deployed clients. JSON is the intended request format; the generated OpenAPI also advertises form and multipart request bodies for the two serializer-backed request types. Responses are JSON unless stated otherwise. Datetimes are server-generated ISO 8601 strings with an offset, for example `2026-08-09T12:34:56.789012+00:00`. UUID values are canonical UUID strings.

### Endpoint Contract Table

| Endpoint | Request body / inputs | Success response | Conditional / notable headers |
| --- | --- | --- | --- |
| Adoption preview | JSON adoption token, installation UUID, app version, P-256 public key, suite | `201` challenge object | None |
| Adoption complete | JSON request UUID, Base64 HMAC proof, `confirmed: true` | `201` installation UUID, one-time credential, lease deadline | None |
| Reactivation preview | Same as adoption preview, but reactivation token | `201` challenge object | Preview itself is unauthenticated |
| Reactivation complete | Same as adoption complete | `201` replacement credential and lease deadline | Requires the current installation credential |
| Check-in | No body | `200` active status, server time, renewed lease deadline | Only this endpoint renews a lease |
| Status | No body | `200` stored status, lease deadline, purge directive | Stale and revoked credentials can read this surface; replaced credentials cannot authenticate |
| Configuration | No body | `200` installation/tablet/department/station/vehicle UUIDs | Requires an active authorized assignment |
| Signing key | Path `version` | `200` algorithm, version, Base64 raw public key | The requested version must be the configured current version |
| Manifest | No body | `200` signed manifest, `202` pending problem, or `304` | `If-None-Match`, quoted `ETag`; pending includes `Retry-After: 5` |
| Download | UUID path parameter | `200` ciphertext bytes or `304` | `If-None-Match`, quoted ciphertext-hash `ETag`; `200` is `application/octet-stream` |

## Installation Authentication

Authenticated tablet endpoints require exactly:

```http
Authorization: Bearer <opaque-installation-credential>
```

The credential is returned once by a successful adoption/reactivation completion. It is a URL-safe random 256-bit credential (`secrets.token_urlsafe(32)`; current tests observe 43 characters). Store it only in iOS Keychain with an appropriate device-only accessibility class. Do not send an installation ID alongside it: the server intentionally looks up credentials without a client-provided installation selector.

Credentials are rotated by reactivation. A replacement adoption marks prior active/stale installations for that tablet as `REPLACED`; tablet removal marks active/stale installations `REVOKED`. The authenticator excludes `REPLACED` credentials, but does not itself reject stale or revoked credentials. The protected endpoint's business authorization supplies that further rejection where applicable.

Malformed, missing, or invalid credentials are handled by DRF and the global problem handler. Do not infer a stable distinction among these cases from the HTTP status alone; treat any `401`/`403` response as needing a status check or reactivation/user action.

## Problem Responses

DRF-generated errors are normalized to RFC 9457-style `application/problem+json`:

```json
{
  "type": "https://fire-backend.internal/problems/tablet-authorization",
  "title": "Forbidden",
  "status": 403,
  "detail": "Installation is not active.",
  "request_id": "4c476bcc-8c0d-49f1-b10e-d1084f4cf719"
}
```

`type` has the server error code as its final path component. Validation errors can have other codes (for example `invalid`); `request_id` is present when request middleware assigned one, otherwise it may be an empty string. Log it for server-side support correlation. The API does not publish a closed error-code enumeration.

The one endpoint-specific problem is manifest pending:

```http
HTTP/1.1 202 Accepted
Content-Type: application/problem+json
Retry-After: 5
```

```json
{
  "type": "https://fire-backend.internal/problems/manifest-pending",
  "title": "Manifest pending",
  "status": 202,
  "detail": "The authorized manifest is being prepared.",
  "request_id": "4c476bcc-8c0d-49f1-b10e-d1084f4cf719",
  "manifest_request_id": "a3f963d0-78c4-4138-b96b-6fb99caa7e6b"
}
```

Honor `Retry-After: 5`; requests for the same authorized state coalesce server-side. A `202` is not a failed manifest or a signal to discard an already verified local dataset.

### Error Action Matrix

| Response / condition | Client action |
| --- | --- |
| `202` manifest pending | Keep the verified cache and wait at least the returned `Retry-After` value before retrying. |
| `304` manifest or download | Use only the previously fully verified cached object; responses have no body. |
| `400` validation problem | Correct local request construction; do not retry unchanged input. |
| `401` or `403` problem | Query status when the credential still authenticates; otherwise require operator/user recovery. Do not treat status alone as a stable malformed-versus-invalid credential classifier. |
| `404` signing key or download | Do not guess another path or key version. Fetch a fresh verified manifest for download authorization; treat an unavailable signing version as a trust failure. |
| `5xx` or transport failure | Retry with bounded exponential backoff and jitter. |
| HPKE, Ed25519, Base64, size, hash, or AES-GCM validation failure | Reject the affected response/artifact, retain no newly accepted cache entry, and fetch a fresh manifest only after investigating persistent failures. |

## Adoption

Before a server administrator creates an invitation, the tablet must be associated with an active department and current active vehicle assignment. The invitation token is out-of-band provisioning data and expires after 15 minutes. It is single-use only after successful completion; a failed proof does not consume it.

### 1. Generate and retain the HPKE key pair

Generate one P-256 (`secp256r1` / `prime256v1`) key-agreement pair when the installation is first provisioned. The backend protocol does not require or attest to Secure Enclave use; use it only when the chosen iOS key-agreement API can export the required ANSI X9.62 public representation and can perform the required HPKE private-key operation. The public key sent to FireDash must be exactly the 65-byte ANSI X9.62 uncompressed point encoding: leading byte `0x04`, then 32-byte X and 32-byte Y. Base64-encode those 65 bytes for JSON. Compressed points, PEM, DER, and JWK are rejected.

The suite string must be exactly:

```text
DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM
```

The server stores the public key fingerprint as lowercase SHA-256 hex of those 65 canonical key bytes. Preserve the private key and the generated `installation_uuid` for the lifetime of this installation. A reactivation must use the same installation UUID and identical public-key bytes.

### 2. Preview request and challenge

`POST /api/v1/adoption/preview`

```json
{
  "token": "synthetic-out-of-band-adoption-token",
  "installation_uuid": "70d3e8aa-d9cc-44fd-9c20-85c0b7f57d87",
  "app_version": "2.4.0",
  "hpke_public_key": "BP6MGc4JBRkevCmKkkV5JTHybwzs4kYGOei8Oct/cGqCanebTPlpuKDlOcf2L7PTCtaqj4DjDx0Siq/WiiznLqA=",
  "hpke_ciphersuite": "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM"
}
```

`token` is at most 256 characters and whitespace is not trimmed. `app_version` is at most 64 characters. The key string must be strict Base64 and then pass the P-256 point validation. The example public key is a valid test-vector encoding, not a production key.

Successful response (`201`):

```json
{
  "adoption_request_id": "d052c99d-f30d-4cd7-9dc0-25bd0b7294b1",
  "encrypted_challenge": "BASE64_OF_65_BYTE_HPKE_ENC_CONCATENATED_WITH_CIPHERTEXT",
  "expires_at": "2026-08-09T12:39:56.789012+00:00",
  "tablet_id": "10614df6-b7cd-4d9c-ae0b-980a096fad97",
  "hpke_ciphersuite": "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
  "hpke_public_key_fingerprint": "<lowercase-sha256-of-65-public-key-bytes>",
  "mode": "adoption",
  "protocol": "tablet-adoption-v1"
}
```

`encrypted_challenge` is Base64 of `enc || ciphertext`, with no length prefix. For this P-256 KEM, `enc` is the first 65 bytes; `ciphertext` is the remaining bytes. Its plaintext is a random 32-byte nonce. The challenge expires five minutes after preview creation.

Decrypt it with RFC 9180 HPKE using the retained P-256 private key and the exact canonical `info` below. Do not use an empty `info` value.

```json
{"adoption_request_id":"d052c99d-f30d-4cd7-9dc0-25bd0b7294b1","expires_at":"2026-08-09T12:39:56.789012+00:00","hpke_ciphersuite":"DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM","hpke_public_key_fingerprint":"<lowercase-sha256-of-65-public-key-bytes>","installation_uuid":"70d3e8aa-d9cc-44fd-9c20-85c0b7f57d87","mode":"adoption","protocol":"tablet-adoption-v1","tablet_id":"10614df6-b7cd-4d9c-ae0b-980a096fad97"}
```

Construct this ASCII JSON with lexicographically sorted keys and `,`/`:` separators using the preview response plus the submitted `installation_uuid`. `mode` is exactly `adoption` for this endpoint and `reactivation` for the reactivation preview endpoint; it is HPKE-bound, so a challenge cannot cross those flows. Compute `challenge_response = HMAC-SHA-256(key: nonce, message: contextInfoBytes)`. This is 32 raw bytes, then standard Base64 for JSON.

### 3. Complete adoption

`POST /api/v1/adoption/complete`

```json
{
  "adoption_request_id": "d052c99d-f30d-4cd7-9dc0-25bd0b7294b1",
  "challenge_response": "BASE64_OF_32_RAW_HMAC_SHA256_BYTES",
  "confirmed": true
}
```

Successful response (`201`):

```json
{
  "installation_id": "2e1ac726-4611-4a61-b303-6a88abb0b5a3",
  "credential": "synthetic-opaque-credential-returned-once",
  "authorization_valid_until": "2026-08-16T12:34:56.789012+00:00"
}
```

Persist the credential before making authenticated calls. A successful adoption sets the installation active and grants a seven-day authorization lease. An existing active/stale installation for the tablet is replaced atomically.

`confirmed: false` is rejected and does not increment the failed-attempt count. An invalid proof increments both the request and invitation counters. At five failed proofs, the invitation is revoked and the request cannot be completed. A completed request, expired request, used/revoked invitation, unsupported suite, or invalid invitation cannot be retried; begin a new preview with a valid invitation.

## Reactivation

Only a stale installation can be reactivated, with an administrator-generated reactivation invitation (15-minute token). Use the same public key, private key, installation UUID, and suite that were used during adoption.

1. Call `POST /api/v1/tablet/reactivation/preview` with the same body as adoption preview, using the reactivation token. The response shape, challenge expiration, and HPKE suite are the same as adoption preview, but it returns `mode: "reactivation"`; use that value in the canonical context.
2. Call `POST /api/v1/tablet/reactivation/complete` with the same body as adoption completion and the *current* installation credential in `Authorization: Bearer ...`.
3. Confirm the `201` response, replace the Keychain credential with the new one, and resume check-ins/manifests.

The completion endpoint verifies that the request belongs to the authenticated installation. It rotates the credential and restores a seven-day active lease. The former credential no longer verifies.

## Lease And Status Flow

### Check in

`POST /api/v1/tablet/check-in` has no request body.

```json
{
  "status": "active",
  "server_time": "2026-08-09T12:34:56.789012+00:00",
  "authorization_valid_until": "2026-08-16T12:34:56.789012+00:00"
}
```

On success, it renews the lease to seven days from server time. Schedule periodic check-ins conservatively before `authorization_valid_until`; an offline client should retain the last server-provided deadline and not assume local clock authority. An active but expired installation is transitioned to stale on check-in and receives an authorization error. A stale installation needs the reactivation flow, not a retry loop.

### Status

`GET /api/v1/tablet/status`

```json
{
  "status": "active",
  "authorization_valid_until": "2026-08-16T12:34:56.789012+00:00",
  "purge_provisioned_data": false
}
```

`status` is lowercased from the stored values: `active`, `stale`, `revoked`, or `replaced`. If `purge_provisioned_data` is `true`, erase locally provisioned API data and cryptographic credentials. This currently occurs only for `revoked`, not `replaced`.

Use status as a recovery/status surface, not as a lease renewal; only check-in renews the lease.

### Installation State Access Matrix

This matrix records runtime authorization, not an instruction to retain a credential after revocation. `403` means the endpoint rejects the request through authentication or business authorization; normal active-manifest processing can still return `202` while worker work is pending.

| Installation state | Credential authenticates | Status | Check-in | Configuration / manifest / download | Reactivation complete |
| --- | --- | --- | --- | --- | --- |
| `active` | Yes | `200` | `200` when the lease and tablet are operational | Authorized; manifest can be `200`, `202`, or `304` | Rejected: only stale installations may reactivate |
| `stale` | Yes | `200` | `403` | `403` | Allowed only with a matching valid reactivation request and proof |
| `revoked` | Yes | `200`, with `purge_provisioned_data: true` | `403` | `403` | Rejected |
| `replaced` | No | `403` | `403` | `403` | Cannot use the replaced credential |

### Configuration

`GET /api/v1/tablet/configuration`

```json
{
  "installation_id": "2e1ac726-4611-4a61-b303-6a88abb0b5a3",
  "tablet_id": "10614df6-b7cd-4d9c-ae0b-980a096fad97",
  "department_id": "e536bce7-0694-4d15-9d81-37549989a29d",
  "station_id": "8f3a5d59-3401-4ea9-aa3c-bcc7fd663973",
  "vehicle_id": "f447a8a2-e152-4eb7-9d9a-4b0877ea4f2b"
}
```

This is derived from the current authorized vehicle assignment. No current active vehicle/station assignment in the tablet's department is an authorization failure. Refresh configuration after an authorization change and before interpreting a manifest bound to its configuration.

## Manifest Retrieval And Verification

`GET /api/v1/tablet/manifest` returns the manifest only after a worker has created HPKE grants and signed it. Send a cached manifest ETag as `If-None-Match`.

* `200`: parse, verify the Ed25519 signature, then process datasets.
* `202`: response is the manifest-pending problem described above. Wait exactly at least the `Retry-After` duration before retrying. Do not exponentially retry faster than the server request.
* `304`: the ETag matches. Do not expect a body; retain the previously verified manifest.

For `200`, `ETag` is a quoted SHA-256 hex of the manifest payload excluding `generated_at`; it is not the manifest signature. A change only to `generated_at` does not change this ETag.

Example (`200`, abbreviated Base64 values):

```json
{
  "manifest_generation": 1,
  "generated_at": "2026-08-09T12:34:56.789012+00:00",
  "authorization_valid_until": "2026-08-16T12:34:56.789012+00:00",
  "configuration": {
    "installation_id": "2e1ac726-4611-4a61-b303-6a88abb0b5a3",
    "tablet_id": "10614df6-b7cd-4d9c-ae0b-980a096fad97",
    "department_id": "e536bce7-0694-4d15-9d81-37549989a29d",
    "station_id": "8f3a5d59-3401-4ea9-aa3c-bcc7fd663973",
    "vehicle_id": "f447a8a2-e152-4eb7-9d9a-4b0877ea4f2b"
  },
  "datasets": [
    {
      "publication_id": "1c6e744d-e499-47bd-8ea5-f1445feded8a",
      "type": "department_hydrants",
      "scope": "department",
      "version": 7,
      "schema_version": 1,
      "required": true,
      "minimum_app_version": null,
      "artifact_format": "geojson",
       "encrypted_size": 4281,
      "ciphertext_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "content_encryption_algorithm": "AES-256-GCM",
      "content_encryption_nonce": "BASE64_OF_12_BYTES",
      "content_key_wrapped_for_kek": "BASE64_OF_AES_KW_WRAPPED_CEK",
      "content_key_wrapping_algorithm": "AES-KW-RFC3394",
      "content_key_kek_version": "1",
      "artifact_signature": "BASE64_OF_64_BYTE_ED25519_SIGNATURE",
      "artifact_signature_algorithm": "Ed25519",
      "artifact_signing_key_version": "1",
      "download_url": "/api/v1/tablet/datasets/1c6e744d-e499-47bd-8ea5-f1445feded8a/download",
      "key_grant": {
        "scheme": "HPKE",
        "ciphersuite": "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
        "encapsulated_key": "BASE64_OF_65_BYTES",
        "wrapped_content_key": "BASE64_OF_HPKE_AEAD_CIPHERTEXT"
      }
    }
  ],
  "signature": "BASE64_OF_64_BYTE_ED25519_SIGNATURE",
  "signature_algorithm": "Ed25519",
  "signing_key_version": "1"
}
```

### Manifest Signature

Fetch the Ed25519 public key for `signing_key_version` from authenticated `GET /api/v1/tablet/signing-keys/{version}` before accepting the manifest. The response has `algorithm: "Ed25519"`, the requested `version`, and a strict-Base64 32-byte raw `public_key`; verify that the response version equals the manifest version. The web service reads a separate public-only credential and never reads the worker private signing credential. Decode `signature` using strict Base64. Verify it over the ASCII/UTF-8 bytes of the manifest after removing only the `signature` property, then serializing JSON with keys lexicographically sorted and compact separators `,` and `:`. `signature_algorithm` and `signing_key_version` remain in the signed payload, because they are added before the final payload is returned and only `signature` is excluded.

Reject a signature failure, an unsupported signature algorithm/version, malformed Base64, or noncanonical re-serialization. Treat the entire manifest as untrusted until signature verification succeeds.

### Signing-Key Trust Rules

For the current beta, the authenticated TLS connection to the trusted FireDash backend is the trust anchor for initial Ed25519 verification-key distribution; the installation Bearer credential authenticates the tablet to that backend. Verify that the returned key has `algorithm: "Ed25519"`, the exact requested version, strict-Base64 32-byte public-key material, and a version equal to the manifest field, but understand that these metadata checks do not create an independent root of trust. Do not trust a signing key obtained through an untrusted connection and do not assume public-key pinning or an independent signing root is implemented. Verify the manifest Ed25519 signature with the key obtained through that trusted backend connection before using manifest configuration, URLs, grants, or artifact metadata.

The artifact signature uses the same Ed25519 algorithm but is a separate integrity check, verified with the exact `artifact_signing_key_version` from an already verified manifest entry. It does not include `publication_id` and is not a standalone publication-identity or authorization assertion. The manifest signature covers the complete dataset entry, including `publication_id`, download URL, artifact signature, artifact signing-key version, and all artifact metadata; that verified manifest is what binds the artifact signature to a publication. Do not accept an artifact signature or key response detached from a verified manifest.

The runtime currently exposes only the configured current signing-key version. A client that must validate cached artifacts after server key rotation needs a previously trusted old public key; the server cannot currently retrieve historical versions.

### Dataset Key Unwrapping

Each key grant contains an HPKE-encrypted 32-byte content-encryption key (CEK). Decode both grant values using Base64. `encapsulated_key` must be exactly 65 bytes; the remainder is the HPKE ciphertext with its AES-GCM authentication tag. Open it with the retained P-256 private key and the exact HPKE suite.

The RFC 9180 `info` is ASCII canonical JSON, sorted keys and compact `,`/`:` separators:

```json
{"ciphertext_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","installation_id":"2e1ac726-4611-4a61-b303-6a88abb0b5a3","protocol":"firedash-hpke-v1","publication_id":"1c6e744d-e499-47bd-8ea5-f1445feded8a","schema_version":1,"scope":{"dataset_type_code":"department_hydrants","department_id":"e536bce7-0694-4d15-9d81-37549989a29d","station_id":null},"tablet_id":"10614df6-b7cd-4d9c-ae0b-980a096fad97","version_number":7}
```

All fields are authenticated. `dataset_type_code` must match `[a-z][a-z0-9_]{0,99}`; `version_number` and `schema_version` must be positive; `ciphertext_sha256` must be exactly 64 lowercase hex characters. On any HPKE authentication error, reject the dataset and fetch a fresh verified manifest rather than trying altered context values.

### Artifact Download And Decryption

Call the manifest's relative `download_url` against the API origin. Never construct an artifact storage path. The server authorizes the publication again against the currently generated manifest before responding.

`GET /api/v1/tablet/datasets/{publication_id}/download`

Successful response behavior:

```http
HTTP/1.1 200 OK
Content-Type: application/octet-stream
ETag: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
Accept-Ranges: bytes
```

The body is the encrypted artifact ciphertext, not the decrypted dataset. The ETag equals the manifest's `ciphertext_sha256`, quoted. Send it with `If-None-Match` to receive `304`; do not expect a body on `304`. Although the server advertises `Accept-Ranges: bytes`, the application view does not parse a `Range` request or generate `206`/`Content-Range`; use whole-object downloads unless the deployment's protected-file layer contract is separately established.

Before decryption, require both `byteCount == encrypted_size` and `SHA-256(ciphertext) == ciphertext_sha256`. `encrypted_size` is the exact manifest-wire field and is the count of encrypted bytes returned by the download, including the AES-GCM tag. The database/worker field is named `artifact_size`; it has the same value but is not a manifest field. Do not look for `artifact_size` in a tablet manifest. Decrypt with AES-256-GCM using:

* key: the 32-byte CEK returned by HPKE;
* nonce: the strict-Base64 `content_encryption_nonce` from the verified manifest;
* additional authenticated data: `nil` / no AAD;
* ciphertext: downloaded bytes, including the 16-byte GCM authentication tag.

The verified manifest supplies nonsecret artifact signature metadata. As a supplementary integrity check, use the key endpoint for its exact `artifact_signing_key_version` and verify the canonical ASCII/UTF-8 JSON below with lexicographically sorted keys and compact `,`/`:` separators. It covers `content_key_wrapped_for_kek` (named `wrapped_cek` inside this payload), nonce, ciphertext hash/size, scope, schema/version, wrapping/encryption algorithms, and KEK version. It intentionally omits `publication_id`; rely on the verified manifest signature for that binding.

```json
{"ciphertext_sha256":"...","ciphertext_size":4281,"encryption_algorithm":"AES-256-GCM","kek_version":"1","nonce":"BASE64_OF_12_BYTES","schema_version":1,"scope":{"dataset_type_code":"department_hydrants","department_id":"e536bce7-0694-4d15-9d81-37549989a29d","station_id":null},"version_number":7,"wrapped_cek":"BASE64_OF_AES_KW_WRAPPED_CEK","wrapping_algorithm":"AES-KW-RFC3394"}
```

`ciphertext_size` in this signed artifact payload equals manifest `encrypted_size`; the different names are intentional protocol layers. `apps/publications/tests/fixtures/artifact_signature_contract.json` is a deterministic complete interoperability vector, including the canonical payload, raw public key, and signature. Its private seed is test-only material.

## Dataset Formats

The production registry exposes these schema-version-1 artifact types when their department `publications` feature is enabled and the publication is authorized. All are marked `required: true` and currently have `minimum_app_version: null`.

| Type | Scope | `artifact_format` | Plaintext format |
| --- | --- | --- | --- |
| `department_hydrants` | department | `geojson` | UTF-8 GeoJSON `FeatureCollection` with top-level `schema_version` and `source_revision`; point coordinates are `[x, y]`; feature properties are `external_identifier`, `hydrant_type`, `diameter_mm`, `status`. |
| `department_fire_plans` | department | `zip` | Deflated ZIP containing `plans/{uuid}.pdf` and `manifest.json`; the manifest has `source_revision` and `fire_plans` entries with `id`, `sha256`, `page_count`, and `path`. Validate each PDF against the manifest hash before presenting it. |
| `station_personnel` | station | `json` | UTF-8 JSON object with `station_id`, `source_revision`, and `people`; each person has `id`, `display_name`, `incident_commander_eligible`, and nullable `commander_email`. The manifest configuration determines the expected station. |

The registry also contains `test_department_incidents`, but it is explicitly internal-only and is not a production dataset contract.

## Recommended Client State Machine

1. **Unprovisioned**: create/persist a P-256 key pair and installation UUID; receive an out-of-band adoption token.
2. **Preview pending**: send adoption preview. If the server returns an error, do not repeatedly submit a bad proof; correct token/key/suite inputs or obtain a new invitation.
3. **Challenge received**: reconstruct the canonical context from preview values and the retained installation UUID, then decrypt and prove the challenge.
4. **Active**: store the credential, call check-in before the lease deadline, then retrieve configuration and manifest.
5. **Manifest pending**: retain verified cache, wait at least five seconds, then request manifest with ETag.
6. **Manifest ready**: verify Ed25519 signature, then for each dataset validate metadata, unwrap CEK with HPKE, download/cache with ETag, validate ciphertext hash/size, and decrypt only when all artifact metadata is available.
7. **Stale**: stop retrying check-in as a recovery mechanism; obtain a reactivation invitation and run reactivation with the old credential.
8. **Revoked**: when status says `purge_provisioned_data: true`, delete credential, private key, verified manifests, CEKs, and downloaded encrypted/decrypted data; return to unprovisioned UI.

Use network retries only for transport failures and `5xx` results with bounded exponential backoff and jitter. Do not retry a `202` before `Retry-After`, a failed adoption proof, or an HPKE/signature/hash validation failure. Conditional GETs should retain the ETag only after the corresponding body has been fully verified and atomically persisted.

## Known Gaps

The following are implementation facts, not client-side workarounds:

* Signing-key distribution currently serves only the configured active `PUBLICATION_SIGNING_KEY_VERSION`. The server has no historical key ring, so deployments must retain prior public keys in clients or add a key-ring configuration before rotating a key whose old artifacts remain in use.
* OpenAPI now has typed scalar response schemas for completion, check-in, status, configuration, signing-key, and manifest-pending responses. It remains intentionally incomplete for nested manifest/dataset objects, generic RFC 9457 errors, response headers, and exact binary download media behavior; runtime documentation above is authoritative for those details. Reactivation preview is shown unauthenticated, which matches runtime but may surprise integrators.
* No `/api/v1/` endpoint documents paging, server base URL, protocol negotiation, rate limits, CORS, clock-skew policy, or supported minimum client versions. Do not invent any of these behaviors in the client contract.

## Source References

* Routes and schema/docs: `config/urls.py:5-10`, `apps/tablets/api_urls.py:5-26`.
* Authentication, serializers, runtime responses, ETags, and download headers: `apps/tablets/api.py:35-334`.
* Global problem response shape: `config/api.py:6-25`; OpenAPI configuration: `config/settings/base.py:176-186`.
* Invitation/adoption, challenge context, HMAC proof, lease, stale/revocation lifecycle: `apps/tablets/services.py:33-75`, `184-267`, `307-461`, `464-524`.
* Persisted installation/request states: `apps/tablets/models.py:45-164`.
* Fixed HPKE suite, P-256 format, canonical `info`, and seal/open behavior: `apps/publications/hpke.py:15-119`; frozen interoperability fixture: `apps/publications/tests/fixtures/hpke_contract.json`.
* Manifest selection, pending state, canonical signature payload, and state coalescing: `apps/publications/manifests.py:32-207`.
* Key-grant HPKE binding and manifest payload/signature construction: `apps/publications/worker_grants.py:28-35`, `87-130`, `149-271`.
* Artifact AES-GCM encryption/signature metadata and canonical fixture: `apps/publications/artifacts.py:40-111`, `apps/publications/tests/fixtures/artifact_signature_contract.json`.
* Dataset registry/build formats: `apps/publications/registry.py:30-138`, `apps/publications/builders.py:152-253`.
* Behavioral coverage: `apps/tablets/tests/test_api.py`, `apps/tablets/tests/test_provisioning_crypto.py`, `apps/tablets/tests/test_lifecycle.py`, `apps/tablets/tests/test_failure_counters.py`, `apps/publications/tests/test_hpke.py`, and `apps/publications/tests/test_manifests.py`.

## Validation Status

`python manage.py spectacular --validate` completed successfully against the generated OpenAPI 3.1 document. The generated schema now includes typed response objects for adoption/reactivation completion, check-in, status, configuration, signing keys, and the manifest-pending problem.

The configured PostgreSQL/PostGIS suite completed successfully: `pytest --ds=config.settings.test` → **151 passed in 28.12s**. Adoption/reactivation contract tests, tablet lifecycle/API/state-matrix tests, artifact fixture/signature tests, and manifest signature/key-grant tests passed. Ruff, `mypy .`, Django system checks, migration consistency, and OpenAPI validation also passed.
