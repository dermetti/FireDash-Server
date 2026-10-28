# FireDash App — Incremental Document Dataset Provisioning Guide

The backend contract is [Tablet API: Shared Document Generation v2](tablet-api.md#shared-document-generation-v2-contract).
All four datasets use schema 2, the same cryptographic protocol and generic endpoints:

- `department_fire_plans`: `fire_plan` metadata.
- `department_klgv_plans`: `klgv_plan` metadata.
- `department_files` and `station_files`: `file` metadata and complete library/folder snapshots.

The server has one delivery implementation. There is no ZIP fallback, separate Fire Plan
protocol, redirect, or cutover control. The Fake-iPad verifier now uses this common protocol for all four datasets.
See [owner-controlled release acceptance](phase3-document-acceptance.md); this document
does not authorize any installation reset or adoption.

Use one reusable document synchronization engine, with dataset adapters only for metadata
validation and presentation. Discover generations from the signed complete tablet manifest's
`document-manifest-v2` descriptors. Fetch the advertised generic generation URL and validate
the complete signed snapshot. A generation 202 has an empty body and `Retry-After: 5`;
the complete tablet manifest's 202 is a problem JSON response. Do not activate either.

Verify Ed25519 canonical JSON signing, the installation/tablet/scope/version HPKE binding
with domain `firedash-document-generation-hpke-v2`, and the AES-KW generation-wrapped CEKs.
Download only missing ciphertext from each signed `download_path`. An exact ciphertext
ETag can return an empty 304 after authorization. Verify ciphertext length and SHA-256,
AES-256-GCM with the signed nonce and no AAD, then sanitized plaintext SHA-256.
See the API contract for the exact envelope and canonical HPKE info serialization.

Stage every generation independently. Cache by immutable artifact identity after validating
its binding to the new signed generation. Metadata-only changes may reuse ciphertext;
equal PDF hashes in different logical documents do not imply shared identity or access.
Only atomically activate after all mandatory metadata and documents have been verified.
On failure, retain the previously verified complete generation. Remove documents absent
from the new snapshot only after activation. Empty complete snapshots are valid.

Authorization is checked for every request. A cached manifest, guessed UUID, old grant,
or matching ETag cannot override installation revocation, expired lease, assignment change,
department/station boundaries, feature gates or app compatibility. File Library retained
successful generations remain subject to current scope authorization.
