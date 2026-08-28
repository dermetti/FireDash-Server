# FireDash App — Incremental Document Dataset Provisioning Guide

**Audience:** FireDash Tablet/iPad application developer
**Purpose:** Implement the client side of FireDash document-dataset publication v2
**Primary initial dataset:** Department Fire Plans
**Future datasets:** KLGV plans and other server-provisioned PDF collections

---

## 1. Objective

FireDash is moving PDF-backed reference datasets away from monolithic encrypted ZIP publications toward a generic **document dataset** model.

The existing Fire Plan behavior is approximately:

```text
Dataset publication
    ↓
one encrypted ZIP containing all PDFs
    ↓
Tablet downloads complete ZIP
    ↓
decrypt + extract
```

The v2 behavior is:

```text
Dataset publication generation
    ↓
signed complete manifest
    ↓
individually encrypted immutable PDF artifacts
    ↓
Tablet compares artifact identities with local state
    ↓
downloads only missing/new artifacts
    ↓
verifies + decrypts them
    ↓
atomically activates complete generation
    ↓
deletes documents absent from new generation
```

The client implementation should **not be Fire Plan-specific**.

Build one reusable document-dataset synchronization engine that can later handle:

```text
department_fire_plans
department_klgv_plans
future_document_dataset_x
future_document_dataset_y
```

Dataset-specific code should primarily define metadata decoding and presentation, while download, cryptography, integrity checking, generation activation, retry, and garbage collection remain generic.

---

# 2. Current server transition

The server implementation is being introduced in stages.

The server now has support for:

* immutable individually encrypted PDF artifacts;
* reuse of unchanged PDF artifacts between publication generations;
* complete document-generation membership;
* signed v2 manifests;
* one generation key per publication generation;
* one HPKE-wrapped generation-key grant per authorized installation;
* authenticated individual PDF ciphertext retrieval;
* rollback-safe artifact retention;
* reference-aware server cleanup.

The server has deliberately **not yet switched live Fire Plan delivery to v2**.

Therefore the app must become v2-capable before the server performs the explicit cutover.

Existing v1 ZIP support must remain functional during the transition.

---

# 3. Do not build a Fire Plan-specific sync subsystem

The desired app architecture is:

```text
Publication discovery
        |
        v
Dataset capability/router
        |
        +------------------------------+
        |                              |
        v                              v
legacy archive handler          document dataset v2 handler
(v1 ZIP)                        (generic)
                                       |
                                       +-- Fire Plans adapter
                                       |
                                       +-- KLGV adapter
                                       |
                                       +-- future PDF dataset adapter
```

The generic v2 handler should own:

* generation comparison;
* signature verification;
* generation-key handling;
* artifact comparison;
* download scheduling;
* ciphertext verification;
* CEK unwrapping;
* AES-GCM decryption;
* plaintext verification;
* staging;
* atomic activation;
* retry recovery;
* obsolete-file cleanup.

Dataset-specific adapters should own only what differs between dataset types, for example:

* decoding document metadata;
* mapping metadata into local domain models;
* display naming;
* local grouping/indexing;
* dataset-specific validation if required.

---

# 4. Dataset capability registry

The app should explicitly register the publication formats it understands.

Conceptually:

```text
department_fire_plans
    schema 1 -> legacy ZIP handler
    schema 2 -> generic document-dataset handler + Fire Plan adapter

department_klgv_plans
    schema 1 -> legacy/optional ZIP handler if currently supported
    schema 2 -> generic document-dataset handler + KLGV adapter

future_document_dataset
    schema 2 -> generic document-dataset handler + dataset adapter
```

Do not scatter checks such as:

```text
if dataset == fire_plans
```

through download and crypto code.

Prefer a central capability lookup based on at least:

```text
dataset_type
schema_version
```

Possibly also scope type if the API requires it.

---

# 5. Unknown datasets and schema versions

The client must fail safely.

If the server advertises a dataset type that the current app does not understand:

```text
unknown dataset
    ↓
ignore as unsupported
```

If the app knows the dataset but not the schema version:

```text
known dataset
unknown schema
    ↓
do not activate it
    ↓
retain current usable local generation
```

An unsupported publication must never cause the app to:

* delete the existing dataset;
* clear local PDFs;
* mark an incomplete generation active;
* treat an empty parse result as an authoritative empty dataset.

This matters particularly for optional future datasets such as KLGV.

---

# 6. Authoritative unit: dataset generation

The app should model a publication as a **generation of a dataset scope**.

For example:

```text
dataset_type = department_fire_plans
scope        = Department A
version      = 42
publication  = <server publication ID>
schema       = 2
```

The locally active state should be explicit.

Conceptually:

```text
LocalDatasetState
    dataset_type
    scope_identity
    active_publication_id
    active_version
    active_schema_version
```

Do not infer the active generation from:

* newest downloaded file;
* filesystem modification time;
* highest local filename;
* existence of a manifest directory.

The app database/state store should be authoritative.

---

# 7. Three identities must remain separate

Do not collapse these concepts.

## Publication generation identity

Identifies the complete authoritative dataset version.

Example:

```text
Fire Plans publication v42
```

## Canonical document identity

Identifies the logical document.

Example:

```text
Fire Plan 7b7...
```

Its metadata may change over time.

## Immutable artifact identity

Identifies one particular sanitized PDF content version.

Example:

```text
Artifact 33e...
```

This is what determines whether a PDF needs downloading.

A later publication might therefore contain:

```text
Generation 42
Fire Plan X
Artifact A

Generation 43
Fire Plan X
Artifact A
```

if only metadata changed.

Or:

```text
Generation 44
Fire Plan X
Artifact B
```

if the PDF itself changed.

---

# 8. Metadata changes must not trigger downloads

Artifact identity, not metadata equality, determines whether PDF content needs downloading.

Example:

```text
Generation 42:
Plan X
address = "Old Street 1"
artifact = A

Generation 43:
Plan X
address = "New Street 1"
artifact = A
```

The app must update the Fire Plan metadata when generation 43 activates but download **zero PDFs**.

Do not calculate download requirements from:

* address;
* object name;
* coordinates;
* display title;
* external identifier;
* mutable filename.

Use immutable artifact identity.

The server also supplies integrity hashes. Those are verification data, not a substitute for the artifact identity model.

---

# 9. Implemented Fire Plan v2 wire contract

This section is the concrete current server contract for **Fire Plans only**.
Keep the generic engine described elsewhere in this guide; KLGV and future
document datasets do not yet have this wire contract.

## Discovery and endpoints

All endpoints below require `Authorization: Bearer <installation credential>`.
They use the existing installation bearer authentication, compatibility checks,
active-installation lease, and current vehicle-assignment authorization.

| Method | Path | Current behavior |
| --- | --- | --- |
| `GET` | `/api/v1/tablet/fire-plan-generations/{publication_id}/manifest` | Returns the complete v2 manifest and one installation-specific generation-key grant, or `202` while the grant worker prepares it. |
| `GET` | `/api/v1/tablet/fire-plan-generations/{publication_id}/artifacts/{artifact_id}/download` | Returns one referenced encrypted PDF ciphertext. |
| `GET` | `/api/v1/tablet/signing-keys/{version}` | Returns the Ed25519 public key needed to verify a manifest whose `signing_key_version` is not already in the app trust root. |

`publication_id` and `artifact_id` are canonical UUID strings. Artifact paths in
server storage are never part of the API contract.

The normal signed `GET /api/v1/tablet/manifest` advertises an authoritative
document generation as this generic dataset entry (and never advertises a
dormant/non-current build):

```json
{
  "publication_id": "11111111-1111-1111-1111-111111111111",
  "type": "department_fire_plans",
  "scope": "department",
  "version": 42,
  "schema_version": 2,
  "required": true,
  "minimum_app_version": null,
  "artifact_format": "document-manifest-v2",
  "manifest_url": "/api/v1/tablet/fire-plan-generations/11111111-1111-1111-1111-111111111111/manifest"
}
```

The descriptor is intentionally generic: route by `type`, `scope`,
`schema_version`, and `artifact_format`, not by a Fire Plan-only condition.
Existing v1 entries retain their existing shape and ZIP fields unchanged.

The signing-key response is:

```json
{"algorithm":"Ed25519","version":"1","public_key":"<base64-32-byte-key>"}
```

## Successful manifest response

The returned object is a complete generation, never a delta. Its exact
top-level shape is:

```json
{
  "format": "fire-plan-generation-v2",
  "publication_id": "11111111-1111-1111-1111-111111111111",
  "version": 42,
  "schema_version": 2,
  "documents": [
    {
      "fire_plan": {
        "id": "22222222-2222-2222-2222-222222222222",
        "external_identifier": "FP-17",
        "object_name": "Training building",
        "address": "Example Street 7",
        "postal_code": "12345",
        "city": "Exampletown",
        "fsd_location": null,
        "bmz_location": null,
        "rwa_info": null,
        "longitude": 8.123,
        "latitude": 49.456,
        "sha256": "<64 lowercase hex characters>",
        "page_count": 2,
        "path": "plans/22222222-2222-2222-2222-222222222222.pdf"
      },
      "artifact_id": "33333333-3333-3333-3333-333333333333",
      "sanitized_pdf_sha256": "<64 lowercase hex characters>",
      "ciphertext_sha256": "<64 lowercase hex characters>",
      "ciphertext_size": 123456,
      "nonce": "<base64 of 12 bytes>",
      "encryption_algorithm": "AES-256-GCM",
      "wrapping_algorithm": "AES-KW-RFC3394",
      "kek_version": "1",
      "signature": "<base64 Ed25519 artifact signature>",
      "signature_algorithm": "Ed25519",
      "signing_key_version": "1",
      "generation_wrapped_cek": "<base64 of AES-KW wrapped 32-byte CEK>",
      "generation_key_wrapping_algorithm": "AES-KW-RFC3394",
      "download_path": "/api/v1/tablet/fire-plan-generations/11111111-1111-1111-1111-111111111111/artifacts/33333333-3333-3333-3333-333333333333/download"
    }
  ],
  "signature": "<base64 Ed25519 manifest signature>",
  "signature_algorithm": "Ed25519",
  "signing_key_version": "1",
  "generation_key_grant": {
    "scheme": "HPKE",
    "ciphersuite": "DHKEM(P-256,HKDF-SHA256)/HKDF-SHA256/AES-128-GCM",
    "encapsulated_key": "<base64 RFC 9180 P-256 enc>",
    "wrapped_generation_key": "<base64 HPKE ciphertext>",
    "info": {
      "protocol": "firedash-fire-plan-generation-hpke-v2",
      "publication_id": "11111111-1111-1111-1111-111111111111",
      "installation_id": "44444444-4444-4444-4444-444444444444",
      "tablet_id": "55555555-5555-5555-5555-555555555555",
      "scope": {
        "dataset_type_code": "department_fire_plans",
        "department_id": "66666666-6666-6666-6666-666666666666",
        "station_id": null
      },
      "version_number": 42,
      "schema_version": 2
    }
  }
}
```

`fire_plan` is the frozen distributed metadata. `fire_plan.id` is the canonical
Fire Plan identity; `artifact_id` is the immutable PDF identity. The `path`
inside `fire_plan` is inherited frozen v1 metadata and is **not** a download
URL or an artifact identity. `documents` are serialized in ascending canonical
Fire Plan UUID order. The server rejects duplicate snapshot plan IDs and
requires exactly one reference for every snapshot plan.

All hashes are lowercase hexadecimal SHA-256 strings. UUIDs are strings.
Binary values (`nonce`, signatures, HPKE fields, and wrapped keys) use standard
padded Base64. `ciphertext_size` and `version` are JSON numbers. A document's
`sanitized_pdf_sha256` must equal `fire_plan.sha256`.

## Signature and cryptographic verification

The manifest signature is Ed25519 over the UTF-8/ASCII bytes of the unsigned
top-level manifest: remove only `signature`, `signature_algorithm`,
`signing_key_version`, and `generation_key_grant`, then JSON-serialize the
remaining object with lexicographically sorted keys, separators `,` and `:`,
and ASCII output (the server equivalent is `json.dumps(payload,
sort_keys=True, separators=(",", ":")).encode("ascii")`). Verify before using
any document metadata or download path. The response-only grant is not signed
as part of that manifest payload.

Each PDF ciphertext was encrypted with a random 32-byte CEK and a random
12-byte nonce using `AES-256-GCM` with **no AAD** (`None`/empty authenticated
data). Verify ciphertext byte count and SHA-256 first; AES-GCM decrypt using
the unwrapped CEK and decoded nonce; then verify SHA-256 of the plaintext PDF
against `sanitized_pdf_sha256`.

`generation_wrapped_cek` is RFC 3394 AES Key Wrap of the 32-byte artifact CEK
under the 32-byte generation key. The resulting wrapped value is normally 40
bytes before Base64 encoding. `wrapping_algorithm` and `kek_version` describe
the server-side KEK wrapping of the artifact CEK; the Tablet uses the separate
`generation_wrapped_cek` and `generation_key_wrapping_algorithm` instead.

The HPKE grant uses RFC 9180 `DHKEM(P-256, HKDF-SHA256) / HKDF-SHA256 /
AES-128-GCM`. `encapsulated_key` is the P-256 uncompressed-point `enc` value
(65 bytes before Base64); `wrapped_generation_key` is the HPKE ciphertext.

The `generation_key_grant.info` object is the complete public HPKE binding.
Validate that its publication/version/schema/type/scope match the signed
manifest and that installation/tablet IDs match the authenticated local
installation. Serialize it as ASCII JSON with sorted keys and separators `,`
and `:`; that exact byte string is RFC 9180 HPKE `info`. Its explicit domain
separator is `firedash-fire-plan-generation-hpke-v2`. It contains no KEK,
server ciphertext, filesystem, or database-only value. Changing publication
or installation identity changes `info` and makes HPKE opening fail.

## HTTP outcomes and authorization

Manifest retrieval returns `200` only with a READY generation-key grant. It
returns `202 Accepted` with `Retry-After: 5` while the grant is pending or
running. Unknown generations return `404`. Authentication, revoked/replaced or
expired installations, missing vehicle assignment, wrong department/scope, and
other authorization failures return an RFC 9457
`application/problem+json` `403`; an incompatible app can receive `426`.

Artifact retrieval requires both the same generation authorization and a READY
grant for that installation/publication. The requested artifact must be a
server-side reference of that publication; unknown or unreferenced IDs return
`404`, while a missing/non-ready grant returns `403`. A valid request returns
`200 application/octet-stream`, `ETag: "{ciphertext_sha256}"`, and immutable
ciphertext. `If-None-Match` returns `304` with the same ETag. The endpoint does
not implement HTTP Range/resume.

Continue to consult `/api/v1/schema/` and `/api/v1/docs/` for the deployed
revision, but the facts above are the local Stage A-D implementation contract.

---

# 10. Treat every manifest as a complete desired state

Given:

```text
Active generation:
A
B
C

New manifest:
A
B
D
```

derive:

```text
A -> keep/reuse
B -> keep/reuse
D -> obtain
C -> remove after successful activation
```

Do not require the server to tell the app explicitly:

```text
ADD D
DELETE C
```

The complete manifest is authoritative.

This makes skipped generations safe.

For example, the app may go directly:

```text
v40 -> v47
```

without applying:

```text
v41
v42
v43
v44
v45
v46
```

---

# 11. Generation-key model

A v2 publication has one random **generation key**.

Each authorized Tablet receives that key through one HPKE-wrapped grant.

The generation key does not directly encrypt the PDFs.

Instead:

```text
Tablet installation private key
        |
        | HPKE unwrap
        v
Generation key
        |
        | unwrap
        v
Artifact CEK
        |
        | AES-256-GCM decrypt
        v
PDF
```

Each artifact has its own random CEK and nonce.

This avoids one HPKE grant per PDF while keeping each PDF independently encrypted.

---

# 12. Required crypto sequence

For a new artifact:

## Step 1 — Download ciphertext

Fetch the artifact through the authenticated API using the server-provided artifact identity.

Do not construct filesystem paths or trust filenames supplied as metadata.

## Step 2 — Verify ciphertext

Before considering the download valid, check the server-provided:

```text
ciphertext size
ciphertext SHA-256
```

A mismatch must fail the artifact.

## Step 3 — Obtain generation key

Use the installation's existing HPKE credentials and the generation-specific grant.

Failure to unwrap the generation key invalidates the sync attempt.

## Step 4 — Unwrap artifact CEK

Use the generation key and the wrapping metadata supplied by the signed manifest.

## Step 5 — AES-GCM decrypt

Use:

```text
artifact CEK
artifact nonce
authenticated metadata/AAD required by contract
```

Do not implement alternative crypto behavior from assumptions. Follow the server's contract exactly.

## Step 6 — Verify plaintext

Calculate SHA-256 over the resulting sanitized PDF bytes.

It must equal the manifest's sanitized plaintext SHA-256.

Only then is the local artifact **verified**.

---

# 13. Trust the signed manifest, not individual HTTP metadata

The cryptographic trust chain should conceptually be:

```text
FireDash signing public key
        ↓
signed generation manifest
        ↓
artifact IDs / hashes / sizes / crypto metadata
        ↓
downloaded ciphertext
        ↓
verified/decrypted PDF
```

Do not accept a conflicting HTTP header, filename, or local cache record over signed manifest data.

Manifest signature verification must occur before its document list is treated as authoritative.

---

# 14. Local artifact state

The app needs persistent knowledge of verified artifacts.

Conceptually:

```text
LocalDocumentArtifact
    artifact_id
    plaintext_sha256
    ciphertext_sha256
    local_pdf_location
    verification_state
```

Additional fields are implementation-specific.

The essential invariant is:

```text
artifact_id is reusable
only if the app knows its local PDF passed full verification
```

A file merely existing at a path does not make it valid.

---

# 15. Ciphertext storage is optional

The server delivers encrypted ciphertext.

After successful verification/decryption, the app may:

* retain ciphertext;
* discard ciphertext and retain only the verified PDF;
* follow the app's existing local-at-rest storage policy.

The important requirement is to persist enough verified artifact identity state to know later that:

```text
Artifact A
```

is already available locally and does not require another download.

Do not retain CEKs or generation keys longer than needed unless the existing app security architecture explicitly requires persistent key storage.

---

# 16. Staging model

Never update the operational Fire Plan/KLGV dataset incrementally while downloads are still happening.

Instead use:

```text
ACTIVE generation
STAGING generation
```

Example:

```text
ACTIVE
v42
A B C

STAGING
v43
A B D
```

During sync:

```text
operational UI -> v42
background sync -> prepares v43
```

Only once v43 is complete:

```text
atomic activation
v42 -> v43
```

---

# 17. Atomic activation

Activation is the critical safety boundary.

Before activation verify:

```text
manifest valid
generation grant valid
every document represented
every required artifact locally verified
dataset-specific metadata valid
local DB/files ready
```

Then perform the smallest possible atomic local transaction that changes:

```text
active_generation = new_generation
```

and associated generation metadata.

Operational readers should see either:

```text
complete old generation
```

or:

```text
complete new generation
```

Never a mixture.

---

# 18. Deletion happens after activation

Consider:

```text
v42:
A B C

v43:
A B
```

Do not delete C when v43 is discovered.

Correct sequence:

```text
discover v43
    ↓
prepare A/B
    ↓
verify complete v43
    ↓
activate v43
    ↓
C no longer belongs to active generation
    ↓
garbage collect C
```

If v43 sync fails:

```text
v42 remains active
C remains available
```

---

# 19. Changed document

Example:

```text
v42:
Plan X -> Artifact A
Plan Y -> Artifact B

v43:
Plan X -> Artifact C
Plan Y -> Artifact B
```

Required network activity:

```text
Artifact C -> download
Artifact B -> reuse

Total PDF downloads: 1
```

After successful activation, Artifact A may become locally unreferenced and may be deleted according to the client's local rollback/cache policy.

For the initial FireDash product behavior, obsolete PDFs should be removed after replacement activation rather than maintaining a large client-side historical cache.

---

# 20. Metadata-only generation

Example:

```text
v42:
Plan X metadata M1
Plan X artifact A

v43:
Plan X metadata M2
Plan X artifact A
```

Required network activity:

```text
PDF downloads: 0
```

The new manifest still needs verification and activation because the authoritative metadata changed.

---

# 21. Added document

Example:

```text
v42:
A B C

v43:
A B C D
```

Required:

```text
download D
verify D
stage v43
activate v43
```

Existing A/B/C artifacts are reused.

---

# 22. Removed document

Example:

```text
v42:
A B C

v43:
A B
```

Required:

```text
downloads: 0
activate complete v43
delete C afterward
```

---

# 23. Interrupted sync

Suppose v43 requires:

```text
D
E
F
```

and the app successfully downloads D and E before connectivity disappears while downloading F.

The app may retain:

```text
verified D
verified E
```

for the next sync attempt.

It must not retain F as a valid artifact unless F completed all verification.

On retry:

```text
D -> reuse staged verified artifact
E -> reuse staged verified artifact
F -> download again
```

This provides useful retry behavior without HTTP Range support.

---

# 24. Partial downloads

Partial artifact files should use a temporary/staging identity distinct from verified artifacts.

For example:

```text
artifact-id.download
```

rather than the final local artifact path.

Promote to verified local state only after:

```text
complete HTTP transfer
+
size verification
+
ciphertext SHA verification
+
successful decryption
+
plaintext SHA verification
```

A crash must not transform a partial artifact into a valid artifact merely because the temporary file remains on disk.

---

# 25. First Fire Plan v2 synchronization

There will be no sophisticated v1-to-v2 local migration.

Assume the Tablet currently has:

```text
Fire Plans v1
60 PDFs extracted from ZIP
```

The first v2 manifest contains approximately 60 individual artifact IDs.

The app has no verified v2 artifact records.

Therefore:

```text
required artifacts: 60
locally known v2 artifacts: 0

download: 60
```

After successful verification:

```text
activate Fire Plan v2
```

Future updates become incremental.

Do **not** hash old v1 extracted PDFs in an attempt to adopt them into v2.

---

# 26. Temporary v1 compatibility

Do not delete the existing v1 ZIP reader when implementing v2.

During the transition, the server may still have protected historical v1 publications that are valid rollback targets.

The app therefore temporarily needs:

```text
schema v1 -> existing ZIP path
schema v2 -> generic document-dataset path
```

This does not mean the server will publish both formats for every generation.

New Fire Plan publication after cutover will use v2 only.

The v1 reader exists temporarily for compatibility with historical rollback state.

Removal of the v1 reader will be a later explicit decision.

---

# 27. Fire Plans adapter

The Fire Plan adapter should map the generic document manifest entry into the app's existing Fire Plan domain model.

It should not own:

* HTTP downloads;
* crypto;
* artifact hashes;
* generation comparison;
* staging state;
* activation;
* cleanup mechanics.

Conceptually:

```text
Generic manifest entry
    |
    +-- canonical document ID
    +-- artifact
    +-- metadata payload
              |
              v
       FirePlanAdapter
              |
              v
       local FirePlan model
```

The exact metadata fields must follow the server's actual Fire Plan API contract.

---

# 28. KLGV support

KLGV should reuse the same generic engine.

Today it should be treated as a separate dataset capability.

Conceptually:

```text
department_klgv_plans
```

When the server eventually introduces KLGV document manifest v2, the app should need only:

```text
KLGV metadata decoder
+
dataset registration
```

not another implementation of:

```text
downloads
encryption
hash verification
activation
garbage collection
```

Desired future structure:

```text
DocumentDatasetSyncEngine
        |
        +-- FirePlanAdapter
        |
        +-- KLGVAdapter
        |
        +-- FuturePdfAdapter
```

---

# 29. Future PDF-backed datasets

A new PDF-backed dataset should be supportable if it can provide:

```text
dataset type
scope
publication/generation identity
complete manifest
canonical document IDs
dataset-specific metadata
immutable artifact IDs
artifact crypto/integrity metadata
```

Adding that dataset should not require changing the generic synchronization engine.

This should be treated as an architectural acceptance criterion for the app implementation.

---

# 30. Do not use document filenames as identity

A PDF's display filename may be useful to users but must not control synchronization.

Avoid logic such as:

```text
if FileManager.fileExists("Plan123.pdf")
    skip download
```

Use:

```text
artifact_id
+
verified local artifact record
```

A renamed Fire Plan with unchanged PDF content should still reuse the same artifact.

---

# 31. Concurrency

Only one synchronization operation for the same:

```text
dataset_type + scope
```

should mutate staging/activation state at once.

Different datasets may be synchronized independently if the app architecture already supports that safely.

Examples:

```text
department_fire_plans / Department A
department_klgv_plans / Department A
station_personnel / Station X
```

Do not allow two Fire Plan sync tasks to race activation or garbage collection.

---

# 32. Network behavior

Artifact downloads should be individually retryable.

A failure downloading:

```text
Plan 57
```

should not require redownloading already verified:

```text
Plans 1–56
```

No HTTP Range/resume behavior is required for the initial implementation.

Use the existing application networking/authentication stack.

Do not construct absolute artifact URLs from assumptions. Follow the links/identifiers defined by the server API contract.

---

# 33. Authentication and authorization failures

Treat authorization failures differently from transient networking failures where practical.

Examples:

```text
timeout / temporary offline
    -> retry later
    -> keep old generation active

401 / invalid or missing installation credential
    -> fail sync
    -> preserve existing local operational data according to current app policy

403 / revoked, expired, replaced, cross-scope, or otherwise unauthorized installation
    -> fail sync
    -> do not repeatedly download artifacts
    -> preserve existing local operational data according to current app policy
```

Do not delete locally active datasets merely because server authorization becomes unavailable.

Existing FireDash installation/revocation policy remains authoritative.

---

# 34. Manifest validation errors

Any of these should reject the candidate generation:

```text
invalid signature
unsupported schema
wrong dataset type
wrong scope
duplicate canonical document IDs
duplicate/inconsistent generation entries
missing required artifact metadata
invalid crypto metadata
malformed document metadata
```

The result is:

```text
candidate rejected
old generation remains active
```

Never partially import a malformed complete manifest.

---

# 35. Artifact validation errors

These must fail the candidate generation:

```text
wrong ciphertext size
ciphertext SHA mismatch
CEK unwrap failure
AES-GCM authentication failure
plaintext SHA mismatch
invalid PDF according to existing local acceptance rules
```

Do not substitute the previous version of that same document into the new generation.

If the new authoritative generation says:

```text
Plan X -> Artifact B
```

and B cannot be obtained, the generation is incomplete.

Continue using the complete previous generation.

---

# 36. Rollback

The server may later make an older retained publication current again.

The app should treat that exactly like any authoritative generation transition.

Example:

```text
currently active v44
server rolls back to v42
```

If v42 references artifacts still available locally:

```text
reuse them
```

If some were garbage-collected:

```text
download them again
```

Then:

```text
verify complete v42
atomically activate v42
```

Do not rebuild historical state from current metadata.

The manifest defines that historical generation.

---

# 37. Local garbage collection

After activation, determine which verified local artifacts are no longer referenced by any locally required generation.

For the initial implementation it is acceptable to retain only what is needed for:

```text
currently active generation
+
currently staged generation
```

unless the existing app already has a defined local rollback cache.

The server is responsible for retaining historical rollback publications. The Tablet can redownload an artifact if a server rollback later requires it.

---

# 38. Suggested internal interfaces

Names are illustrative.

```text
DocumentDatasetSyncEngine

sync(publication)
verifyManifest(...)
prepareGeneration(...)
activateGeneration(...)
garbageCollect(...)
```

```text
DocumentDatasetAdapter

datasetType
supportedSchemaVersions

decodeDocumentMetadata(...)
validateDocument(...)
applyGenerationMetadata(...)
```

```text
ArtifactStore

containsVerifiedArtifact(id)
stagedArtifact(id)
saveVerifiedArtifact(...)
removeArtifact(...)
```

```text
DocumentCrypto

verifyManifest(...)
unwrapGenerationKey(...)
unwrapArtifactKey(...)
decryptArtifact(...)
verifyPlaintext(...)
```

Avoid exposing Fire Plan-specific concepts through these generic interfaces.

---

# 39. Suggested synchronization pseudocode

```text
sync(candidate):

    handler = capabilityRegistry.handler(
        candidate.datasetType,
        candidate.schemaVersion
    )

    if handler unsupported:
        return unsupportedWithoutChangingActiveState

    if candidate.schemaVersion == legacyV1:
        return existingLegacySync(candidate)

    manifest = fetchManifest(candidate)

    verifyManifestSignature(manifest)

    validateManifestIdentity(
        datasetType,
        scope,
        publicationId,
        version
    )

    generationKey = obtainAndUnwrapGenerationGrant(candidate)

    staging = beginStagingGeneration(candidate)

    for document in manifest.documents:

        artifact = document.artifact

        if artifactStore.containsVerified(artifact.id):
            attachExistingArtifact(staging, document)
            continue

        if artifactStore.containsVerifiedStaged(artifact.id):
            attachExistingArtifact(staging, document)
            continue

        ciphertext = downloadArtifact(artifact.id)

        verifyCiphertext(
            size,
            sha256
        )

        cek = unwrapArtifactCEK(
            generationKey,
            artifact.wrappedCEK
        )

        plaintext = decryptAESGCM(
            ciphertext,
            cek,
            artifact.nonce
        )

        verifyPlaintextSHA256(
            plaintext,
            artifact.plaintextSHA256
        )

        saveVerifiedArtifact(
            artifact.id,
            plaintext
        )

        attachArtifact(staging, document)

    validateGenerationComplete(staging, manifest)

    atomicallyActivate(staging)

    garbageCollectArtifactsNotRequiredByActiveGeneration()
```

Every failure before `atomicallyActivate()` must leave the old active generation unchanged.

---

# 40. User-visible behavior

The operational UI does not need to expose the internal cryptographic design.

Useful sync states may remain simple:

```text
Up to date
Updating
Offline
Update failed
```

If existing UX shows progress, v2 can report something meaningful such as:

```text
Updating Fire Plans
2 of 3 changed documents downloaded
```

Do not present a metadata-only update as downloading dozens of PDFs when no PDFs are transferred.

UI changes are secondary to sync correctness.

---

# 41. Logging and diagnostics

Provide enough structured diagnostic information to distinguish:

```text
manifest fetch failure
manifest verification failure
generation grant failure
artifact authorization failure
artifact download failure
ciphertext hash failure
CEK unwrap failure
AES-GCM failure
plaintext hash failure
local persistence failure
activation failure
cleanup failure
unsupported dataset/schema
```

Never log:

* private HPKE keys;
* generation keys;
* document CEKs;
* wrapped-key plaintext;
* decrypted sensitive document contents.

Artifact/publication IDs and dataset types are sufficient for most diagnostics.

---

# 42. Required Fire Plan acceptance scenarios

Before the server enables Fire Plan v2, the app must demonstrate these scenarios.

### Initial v2 sync

```text
existing local v1 Fire Plans
+
first v2 generation with ~60 PDFs

expected:
all v2 PDFs downloaded once
v1 remains active until complete
v2 activates atomically
```

### Metadata-only change

```text
same artifact IDs
different metadata

expected PDF downloads:
0
```

### One changed PDF

```text
one changed artifact ID

expected PDF downloads:
1
```

### One new Fire Plan

```text
one new artifact ID

expected PDF downloads:
1
```

### One removed Fire Plan

```text
new manifest omits document

expected downloads:
0

expected deletion:
only after successful activation
```

### Interrupted sync

```text
some new artifacts complete
one fails

expected:
old generation stays active
verified staged artifacts may be reused on retry
```

### Corrupt ciphertext

```text
SHA mismatch

expected:
reject artifact
reject candidate generation
old generation remains active
```

### Wrong key/tampering

```text
unwrap/GCM verification fails

expected:
reject generation
old generation remains active
```

### Server rollback

```text
older retained publication becomes authoritative

expected:
reuse locally available artifacts
download missing ones
activate historical generation atomically
```

### Legacy rollback

While v1 compatibility remains required:

```text
server rolls back to protected schema-v1 Fire Plan publication

expected:
existing v1 ZIP reader can still consume it
```

---

# 43. Generic document-engine acceptance scenarios

In addition to Fire Plan tests, create at least one synthetic second document dataset in tests.

It does not need to correspond to live KLGV yet.

The purpose is to prove that the synchronization engine is genuinely generic.

The same engine should successfully process:

```text
dataset A + adapter A
dataset B + adapter B
```

without Fire Plan-specific branching in:

* crypto;
* download;
* artifact cache;
* activation;
* garbage collection.

This will make the later KLGV implementation substantially smaller and safer.

---

# 44. Out of scope for the first implementation

Do not implement:

* server cutover;
* server publication creation;
* v1-to-v2 local PDF adoption;
* HTTP Range requests;
* resumable byte-range downloads;
* peer-to-peer artifact sharing;
* cross-dataset artifact deduplication;
* custom per-document authorization;
* a separate KLGV synchronization engine;
* client-side reconstruction of server manifests;
* incident/intervention uploads or storage.

---

# 45. Cutover contract with the server developer

The server-side cutover must happen **only after this app version is deployed and accepted**.

Expected sequence:

```text
1. Server with dormant v2 capability deployed
2. App with v1 + v2 support deployed
3. App v2 acceptance completed
4. Operator explicitly enables/builds first Fire Plan v2 publication
5. Existing server PDFs become individual publication artifacts
6. Successful v2 generation becomes current
7. Tablet discovers v2
8. Tablet downloads complete first v2 set once
9. Tablet activates it
10. Future publication changes are incremental
```

Deploying server code alone must not trigger the cutover.

---

# 46. API integration rule

Section 9 is the verified Stage A-D Fire Plan v2 contract, including the exact
current paths, JSON members, canonical manifest bytes, encodings, and response
semantics. Still obtain the generated schema from the exact deployed revision:

```text
/api/v1/schema/
/api/v1/docs/
```

Use those endpoints to confirm deployment/version drift and any cutover work.
Confirm that the deployed schema retains the Section 9 document-manifest
descriptor and generation-key-grant `info` object before enabling cutover.

---

# 47. Definition of done

The app-side document provisioning refactor is complete when:

1. one generic document-dataset v2 engine exists;
2. Fire Plans use that engine;
3. existing Fire Plan v1 ZIP support still works during transition;
4. first v2 sync safely downloads a complete individual-PDF generation;
5. metadata-only updates download zero PDFs;
6. one changed PDF causes one PDF download;
7. additions download only new PDFs;
8. removals are deleted only after successful activation;
9. interrupted/corrupt/unauthorized sync cannot replace the current complete generation;
10. immutable artifacts are reused by server artifact identity rather than filename/metadata;
11. manifest, HPKE grant, CEK wrapping, AES-GCM, and SHA verification follow the server contract;
12. rollback works for retained v2 generations;
13. the design can support KLGV through a dataset adapter instead of another synchronization subsystem;
14. a synthetic second document dataset proves the core engine is not Fire Plan-specific;
15. unsupported future datasets/schema versions fail safely without damaging active local data.

The central invariant is:

```text
A document-dataset generation becomes operational only when
the complete signed generation is locally ready and verified.
```

Everything else—incremental downloads, safe deletion, rollback, KLGV support, and future PDF provisioning—builds on that rule.
