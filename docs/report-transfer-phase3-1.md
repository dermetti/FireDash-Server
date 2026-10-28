# Report Transfer, Phase 3.1

**Historical Phase 3.1 contract:** [Phase 3.2](report-transfer-phase3-2.md) now
requires ReportDocument v1 inside the same encrypted transport and supersedes the
provisional retention and attachment defaults below. Stored development packages
retain exact replay/status compatibility; see the Phase 3.2 migration strategy.

This local implementation covers RPT-001, RPT-010, RPT-020, RPT-030 and
RPT-060–062 from `misc_md_files/v1.0 Architecture.md`. It accepts an entire
encrypted package in one request. There is no email, portal, PDF export or
publication operation. The existing PDF-Mail endpoint and configuration field
remain operational.

## HTTP contract (development version 1)

Both endpoints use the existing installation Bearer credential, app compatibility
policy, persisted active installation/tablet/department, unexpired lease and
current active vehicle/station assignment. HTTPS is required outside isolated
local tests. IDs are lowercase, hyphenated UUID strings. No client email address
is accepted. Recipient eligibility, verified email and department domain policy
are resolved by the existing `resolve_report_recipient` function on every POST,
including replay.

`POST /api/v1/tablet/report-submissions`, `Content-Type: application/json`:
the body is exactly an RFC 7516 **flattened JWE JSON serialization** with five
string members:

```json
{"protected":"BASE64URL_HEADER","encrypted_key":"BASE64URL_WRAPPED_CEK",
 "iv":"BASE64URL_NONCE","ciphertext":"BASE64URL_CIPHERTEXT","tag":"BASE64URL_TAG"}
```

All base64url values use the URL alphabet without padding and must be canonical.
The UTF-8 JSON protected header has exactly these members:

```json
{
  "typ": "firedash-report+jwe",
  "alg": "RSA-OAEP-256",
  "enc": "A256GCM",
  "kid": "1",
  "package_version": 1,
  "schema_version": "transport-json-v1",
  "submission_id": "CLIENT_GENERATED_UUID",
  "report_id": "CLIENT_GENERATED_UUID",
  "recipient_personnel_id": "SERVER_PERSONNEL_UUID",
  "installation_id": "AUTHENTICATED_CONFIGURATION_INSTALLATION_UUID",
  "department_id": "AUTHENTICATED_CONFIGURATION_DEPARTMENT_UUID",
  "station_id": "AUTHENTICATED_CONFIGURATION_STATION_UUID"
}
```

Generate a fresh random 32-byte content encryption key (CEK) and random 12-byte
nonce per new package. Wrap the CEK using the configured report RSA public key,
OAEP SHA-256, MGF1 SHA-256 and an empty OAEP label. Encrypt the entire UTF-8 JSON
payload with AES-256-GCM, using **ASCII bytes of the transmitted `protected`
base64url string** as AAD. Split the last 16 bytes from the cryptography AESGCM
result into `tag`; the rest is `ciphertext`. There is no external AAD,
compression, unprotected header, alternate algorithm or truncated tag.

The decrypted package has exactly these members:

```json
{
  "report": {
    "data": "BASE64URL_EXACT_UTF8_JSON_BYTES",
    "size": 2,
    "sha256": "LOWERCASE_HEX_SHA256_OF_JSON_BYTES",
    "attachment_ids": ["ATTACHMENT_UUID"]
  },
  "attachments": [{
    "id": "ATTACHMENT_UUID",
    "report_id": "SAME_REPORT_UUID_AS_HEADER",
    "media_type": "image/jpeg",
    "data": "BASE64URL_EXACT_JPEG_BYTES",
    "size": 1234,
    "sha256": "LOWERCASE_HEX_SHA256_OF_JPEG_BYTES"
  }]
}
```

Sizes and hashes refer to decoded bytes, before base64 encoding. The JSON must
be an object. Duplicate JSON keys, non-finite numbers, unknown envelope/package
members, invalid UUIDs, missing/duplicate/unlisted attachments, mismatched report
membership and invalid hashes/sizes fail closed. Each JPEG is inspected, verified
and fully decoded with Pillow, with a pixel ceiling. Empty attachment and
reference arrays are valid. Attachment order is insignificant to membership,
but retries retain the original encrypted package.

This is a transport schema only. `schema_version: transport-json-v1` explicitly
does **not** claim that a final business report schema exists. The report object
is opaque data; it is never interpreted as HTML, paths or executable content.
Its approved fields, validation rules and later business-schema version must be
defined with the final form before beta. Attachment references live in the
transport wrapper until that schema is agreed.

First acceptance returns **201**; an exact replay returns **200**:

```json
{"submission_id":"UUID","report_id":"UUID","state":"READY",
 "expires_at":"2026-10-31T12:00:00+00:00"}
```

Persist both UUIDs and the encrypted JWE before sending. After a lost response,
retry the same JWE or query status. Do not re-encrypt on retry: new randomness
changes the package digest. JSON member ordering/whitespace in the outer JWE
does not affect replay; its five string values must remain identical.
The installation row lock and database uniqueness constraints serialize claims.
Conflicting reuse of either submission or report identity within an installation
returns **409** `idempotency_conflict`. One report per installation is a reversible
development choice; editing/revision/resubmission semantics remain unapproved.

`GET /api/v1/tablet/report-submissions/{submission_id}/status` returns the same
shape with **200**. Unknown and other installations' IDs both return **404**.
The current department and station must also match the stored submission.
Status does not return recipient details, email, file references, keys or content.
Revoked/expired/unassigned installations cannot query status. Recipient changes
block subsequent POST/replay; status remains technical metadata for the sender.

Only `READY` and `EXPIRED` exist. Expiry is effective at `expires_at`, even before
cleanup runs. Exact replay of an expired submission returns `EXPIRED` without
recreating contents or extending retention. No partial receiving/failed row is
published. No partial-upload resume is implemented: retry the whole package.

Controlled errors return `{"code":"..."}`: **400** `invalid_package` or
`unknown_report_key`; **403** `scope_not_authorized`,
`installation_not_authorized`, or `recipient_not_authorized`; **404**
`submission_not_found`; **409** `idempotency_conflict`; **413**
`package_too_large`; **503** `report_keys_unavailable` or
`report_storage_unavailable`. Existing authentication, compatibility (426),
content negotiation and protocol errors retain their existing response format.
All responses inherit the tablet protocol's no-store cache policy.

## Key distribution and rotation

The existing authenticated `/api/v1/tablet/configuration` now includes
`report_transfer`. The identical object is included in signed manifests and
their state hash/ETag. Version 1 advertises `enabled`, `kid`, `alg`, `enc`,
`public_key_pem` (SPKI PEM), and the limits below. Missing/malformed credentials
advertise only `{"version":1,"enabled":false}`; there is no fallback key.

`REPORT_TRANSFER_KEYRING_PATH` uses the existing deployment credential-path
convention (`report-transfer-keyring`). Its UTF-8 JSON is
`{"keys":{"VERSION":"PKCS8_PRIVATE_PEM_STRING"}}`. RSA keys must be at least
3072 bits. `REPORT_TRANSFER_ACTIVE_KEY` selects the advertised write key.
These are report-only keys, separate from publication HPKE, signing, publication
KEKs and application-secret KEKs. No new runtime dependency is required.

An operator adds a new private key/version to this dedicated protected credential
and changes the active version. Old versions remain decryptable during a
controlled overlap. Remove a version to refuse new packages using that key.
Exact already-validated replays use the stored digest, so they do not require a
retired key. Keep old keys until every live package using them has expired and
been removed if later authorized readers need decryption. A removed version
must never be reused for different key material. Key files must be readable only
by the service identity and must never be committed, logged or returned.

Plaintext and CEKs exist only during in-process validation. They are never saved
to files or model fields. Sensitive validation frames are marked for Django's
error reporter. Python cannot guarantee physical zeroization of immutable byte
objects; process hardening, swap/core-dump policy and telemetry redaction require
deployment review before beta.

The contract follows [JWE (RFC 7516)](https://www.rfc-editor.org/rfc/rfc7516),
[JWA (RFC 7518)](https://www.rfc-editor.org/rfc/rfc7518), and the cryptography
[RSA](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/)
and [AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/)
interfaces.

## Limits and lifecycle

| Setting | Development default |
|---|---:|
| `REPORT_TRANSFER_MAX_REQUEST_BYTES` | 24 MiB, entire encoded HTTP body |
| `REPORT_TRANSFER_MAX_JSON_BYTES` | 1 MiB, decoded report |
| `REPORT_TRANSFER_MAX_JPEG_BYTES` | 4 MiB per decoded JPEG |
| `REPORT_TRANSFER_MAX_ATTACHMENTS` | 4 |
| `REPORT_TRANSFER_MAX_IMAGE_PIXELS` | 16,000,000 per JPEG |
| `REPORT_TRANSFER_RETENTION_HOURS` | 72, provisional |

The whole-request ceiling applies in addition to individual ceilings; base64
overhead means all individual maxima cannot necessarily be combined. The parser
reads at most the whole-request limit plus one byte. The infrastructure should
also bound body size, upload duration, concurrent requests and aggregate spool
capacity. Automatic public admission and per-tenant storage quotas are outside
this local slice.

`REPORT_TRANSFER_ROOT` defaults to `/var/spool/firedash-reports`, deliberately
outside publication artifacts and their backup tree. It contains only encrypted
JWE files with server-generated names, created exclusively and fsynced with
private POSIX permissions (0700 directory, 0600 files). Overlap with configured
publication, media, import and accepted-source storage is rejected. Windows test
storage inherits the disposable scratch directory's ACLs.

Acceptance validates the whole package before writing ciphertext and committing
its row. Write/transaction failure removes its file; hard process death may leave
an orphan. Metadata rows contain identity, recipient FK, scope, ciphertext digest,
key version, timestamps, state and opaque file reference, without payloads or
email snapshots. There is no model relation to DatasetPublication or generations.

Run `python manage.py expire_report_submissions` regularly using the appropriate
environment (tests use only the isolated test settings). It unlinks expired
contents before marking rows EXPIRED and clearing file references. Missing files
are success; unlink failure retains the reference for retry. It also sweeps
unreferenced generated `.jwe` files, including interrupted writes. All writers
and sweepers share a PostgreSQL transaction advisory lock, so an upload awaiting
commit cannot be mistaken for an orphan. Counts are printed; any deletion failure
produces a nonzero command exit. Retrying and concurrent cleanup are safe.

Retained metadata tombstones prevent late retries from recreating expired data.
Their retention horizon, deletion/anonymization policy and retry guarantees after
that horizon remain a product/privacy decision; this slice does not delete them.
The **72-hour content lifetime is a provisional development default**, not a
privacy-approved policy. Scheduling/monitoring cleanup, excluding this spool from
backups, snapshots and file versioning, crash-dump/log policy, restoration with
missing ciphertext and deletion guarantees on the chosen filesystem must be
validated before beta. Path isolation alone cannot enforce an external backup
agent's policy. No deployment or backup configuration was changed here.

## Reproducible local example and focused validation

From the repository root in PowerShell, with the existing `.env` test PostgreSQL
values and installed development dependencies:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests/test_submissions.py::test_accept_replay_status_and_ciphertext_only -v
```

This self-contained example creates a disposable authorized tablet, assignment,
verified commander, temporary RSA credential and private spool. The independent
client implementation in `encrypt()` constructs the exact JWE above. It submits
`{}`-shaped opaque JSON both with and without a real JPEG, asserts 201/READY,
repeats the same envelope and asserts 200 with identical status, then retrieves
status and verifies that only ciphertext exists on disk. No production key,
real email, Fake-iPad state or persistent local tablet setup is needed.

The focused suite additionally covers scope and credential rejection, lease and
authorization revocation, recipient eligibility/email/policy, invalid metadata,
AEAD failures, hashes, actual JPEG decoding, limits, interrupted writes, database
failure, concurrent duplicates, key overlap/retirement, signed configuration
freshness, expiry, failed deletion/retry, missing files, orphan sweeping and
absence of report markers/keys in logs. Existing outbound-mail and tablet API
tests exercise the old PDF-Mail workflow. Test storage and credentials live only
under pytest's guarded `.test-tmp` base and are removed on completion.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py apps/tablets/tests/test_api.py apps/publications/tests/test_manifests.py -q
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/ruff.exe check apps/report_transfer apps/tablets/api.py apps/tablets/api_urls.py apps/tablets/configuration.py apps/tablets/tests/test_api.py config/settings/base.py
```

Local validation completed against isolated PostgreSQL: 151 tests passed in the
combined run (54 Report Transfer and 97 existing tests). After adding rejected-log
coverage and strengthening concurrent duplicate/conflict coverage to use HTTP,
the final Report Transfer run passed all 56 tests. Django checks reported no
issues, migration drift checks reported no changes, Ruff passed, and
`git diff --check` passed. Pytest removed its disposable storage afterward.
No full-suite run, Git write, deployment, live database change, real email,
Fake-iPad mutation or publication cleanup was performed.
