# Architecture

## Product and tenancy boundary

FireDash Server provisions reference data for fire departments and their
stations. It also accepts encrypted, structured report submissions from
authenticated tablets. Report Transfer temporarily stores encrypted packages
and provides controlled recipient access through the report portal. It is
separate from publication and reference-data ingestion. Department and station
scope are enforced in application queries, permissions, and database constraints.

The primary domain records are departments, stations, vehicles, personnel,
historical assignments, tablets and installations, hydrants, accepted fire
plans, dataset publications, and append-only audit events.

## Platform

The server uses Django 5.2, Django REST Framework, PostgreSQL 17 with PostGIS,
Gunicorn behind Nginx, and hardened systemd units on Debian LXC. PostgreSQL is
the authoritative work queue and state store. FireDash does not use Redis,
Celery, Docker, Kubernetes, or a separate queue broker.

## Assignments

Assignments preserve history. `valid_from` is inclusive and `valid_until` is
exclusive, so an assignment is effective at time *t* when
`valid_from <= t < valid_until`; an unset end is open-ended. Ended rows are
historical rather than deleted. A person has exactly one current HOME
assignment; transfers close the prior HOME assignment before opening the new
one. Temporary assignments remain distinct, expire by their effective end,
and do not replace the HOME assignment.

## Publications

Publication scopes are first-class persisted identity: `SYSTEM` has no department or station, `DEPARTMENT` has a department only, and `STATION` has both a department and station. A SYSTEM publication has one global lifecycle and is never copied per department. Scope ownership is deliberately separate from tablet exposure: a usable publication still requires the dataset exposure policy and installation/department authorization before it enters a manifest.

SYSTEM-managed configuration is distinct from SYSTEM-published datasets. Vehicle Rescue Guides is the first such configuration: one system-administered Euro RESCUE HTTPS web URL is delivered as a signed tablet capability to every authorized installation. It has no publication, artifact, build, or per-department copy. SYSTEM datasets retain their own globally authored publication lifecycle; Dangerous Goods is an implemented SYSTEM dataset (`dangerous_goods`, schema 1,
encrypted JSON) with explicit system exposure; SYSTEM ownership alone grants no
distribution authorization. Licensing and offline rescue artifacts are not implemented.

Publication work is split into three operational lanes:

1. **Delivery** is a persistent worker that polls at roughly two seconds and
   generates `DatasetKeyGrant` and `SignedManifest` records only.
2. **Build** processes dataset builds only. Source changes coalesce into one
   queued intent per scope for the nightly 00:05 build timer. An authorised
   Build & publish action can promote that same intent and wake the build
   service through a narrow Unix socket.
3. **Maintenance** performs credential-free retention and artifact cleanup.

`source_revision` tracks internal source changes. A queued intent always means
“build the latest source for this scope”; changes while a build is running
coalesce into one follow-up intent. A successful build is auto-published only
when its source snapshot is still current. Failed or obsolete work leaves the
known-good publication active.

Each build attempt receives one immutable, monotonically increasing version
within its dataset scope. The version is cryptographically bound to that
attempt, including failed and obsolete attempts, so successful/current
versions may contain gaps. Scheduled, manual, and bulk-expedited origins are
recorded separately. `DatasetScopeState.current_published_publication` is the
one authoritative active version. Rollback is an audited, atomic recovery
action to a usable superseded version; active deletion first activates a safe
predecessor and otherwise fails closed. Retention always protects the current
version and live operations. Its default policy protects two newest
READY-metadata superseded rows, not necessarily restorable generations. Opt-in
strict selection uses `PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=True`; see
[configuration](configuration.md#publications) and the
[approval/audit runbook](operations.md#publication-retention-policy-and-audit).
Retirement marks older successful versions `OBSOLETE`, while failed/cancelled
attempts only lose aged snapshots. Historical database identities remain intact.

All four PDF datasets (`department_fire_plans`, `department_klgv_plans`,
`department_files`, `station_files`) use the generic Document Generation v2
service and schema 2. A signed complete generation references immutable,
individually encrypted document artifacts. Generic generation keys and
installation-bound HPKE grants authorize the common `/document-generations/`
manifest and download endpoints. Dataset adapters own frozen metadata, source
membership and accepted-PDF lookup; encryption and delivery remain shared.
There are no document ZIPs or publication-level companion artifacts.

Tablet sync fetches and verifies the
latest signed generation, compares with the locally active generation, reuses
identical local artifacts, downloads only missing/changed artifacts, verifies and
decrypts each required new artifact, then atomically activates the new generation
only after everything required is ready; documents absent from the new manifest
are deleted/gc'd only after activation, and any failed download/decrypt/import
retains the previous complete generation. Deterministic keys/nonces are never
derived for deduplication; deduplication reuses already-built immutable artifacts
when sanitized content is unchanged.

## Source fingerprints, frozen identity and restore

Logical source fingerprints are distinct from revision counters and immutable
publication identities. For File Libraries, equality excludes `source_revision`
and internal `content_id`, but includes stable library/folder/document UUIDs,
names, hierarchy, sanitized PDF hashes and sizes. Frozen snapshots retain the
internal fields needed by pinning/worker validation. Only equality with the
**active** publication cancels redundant ordinary work. Reverting to a
superseded snapshot still requires a new generation; eligible artifacts may be
reused without reusing its generation identity.

Compatibility comparisons derive logical fingerprints from retained active
snapshots, retaining the active publication's original fingerprint as a scope
alias where needed. Missing/incomplete historical snapshots remain conservatively
dirty; counts or revisions cannot establish equality. Immutable publications
are not backfilled. A confirmed Rebuild persists `allow_clean_rebuild=True` on
the existing job model. Ordinary Build Now/bulk requests do not acquire it.
Timeout recovery and source-change replacement retain that intent; successful
activation supersedes normally. Terminal obsolete attempts are not resurrected.

File Library workers freeze complete source under the source/scope/library locks
and pin immutable content before BUILDING commits. Current source mutation cannot
mix revisions into a frozen build. Restore uses exact scope/dataset/owner/schema,
READY state and, for documents, frozen membership, manifest/key/reference and
cryptographic metadata validation. Direct rollback and active-delete predecessor
selection validate strictly under lifecycle locks; UI metadata checks do not
replace them. Historical Fire Plan/KLGV validation does not depend on mutable
accepted files retaining old hashes; complete empty generations remain valid.
Neither strict restore eligibility nor retention selection reads physical
ciphertext or freshly verifies signatures, so storage recovery still requires
independent integrity/backup evidence.

## Canonical data ingestion

Canonical source changes enter through persistent import batches rather than
publication code. An uploaded source is bounded, parsed and (for PDFs)
sanitized into a side-effect-free preview. A human confirmation rechecks the
exact staged SHA-256 and baseline, commits canonical rows atomically, then
marks each unique affected dataset scope dirty once. Imports never create
artifacts, publication attempts, grants, manifests, encryption, or signatures.
The normal build/delivery lanes remain responsible for those steps.

## Tablet cryptography

Each publication has one encrypted artifact. A random AES-256-GCM content
encryption key (CEK) encrypts it; the CEK is protected by the publication KEK.
Each authorised installation receives an HPKE-wrapped CEK grant. Ed25519
signatures authenticate artifacts and manifests.

Gunicorn authorises tablet requests but does not receive the publication KEK
or Ed25519 private signing key. Those credentials are supplied only to the
`fire_publication` worker boundary. The web service has the signing public
key needed to publish verification information. The detailed tablet contract
is in [tablet-api.md](tablet-api.md); security rationale is in
[security.md](security.md).

## Encrypted Report Transfer

See the authoritative [Report Transfer guide](report-transfer.md) and
[tablet wire contract](tablet-api.md#encrypted-report-transfer).

Tablets upload a bounded flattened JWE package containing ReportDocument JSON
and JPEG attachments. The server validates scope, document structure and content
and stores the encrypted submission separately from publications. Transfer
requests snapshot available reports and email a one-use, 60-minute Magic Link;
PDFs are generated on the server for portal downloads.

`apps/outbound_mail/recipients.py` resolves recipients afresh from the authenticated
installation's department and persisted personnel state. Recipients must be
active and commander-eligible, have a verified valid commander email, and satisfy
the department's exact recipient-domain policy. Foreign and ineligible IDs have
the same sanitized rejection. Address construction lives in `addresses.py`;
provider configuration, transport, throttling and audit infrastructure remain shared.
Authorization is rechecked before sending and on portal access, including mailbox
identity and revocation. Overview/detail rendering, generated PDF and JPEG downloads
remain part of the report portal. Retention is at most seven days and shortens
to 48 hours after a completed explicit download.

The pre-beta direct PDF-mail endpoint and its admission/idempotency services
have been removed. `TabletReportDeliveryRequest` remains only as historical
storage to preserve its existing table, records and migration compatibility.
No active service reads or writes it. Removing that model/table is technical
debt requiring separate owner authorization; this retirement creates no migration.
