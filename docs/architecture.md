# Architecture

## Product and tenancy boundary

FireDash Server provisions reference data for fire departments and their
stations. It deliberately has no endpoint or storage model for incident,
intervention, or generic tablet-upload data. Department and station scope are
enforced in application queries, permissions, and database constraints.

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
predecessor and otherwise fails closed. Retention protects the current version
and two usable rollback predecessors, marks older successful versions
`OBSOLETE`, and only purges aged source snapshots for `FAILED`/`CANCELLED`
attempts without changing their terminal status or permanent attempt identity.

Current required production dataset types are `department_hydrants`,
`department_fire_plans`, and `station_personnel`. The internal,
feature-disabled `department_klgv_plans` registry entry proves additive v1
dataset evolution: it is department-scoped, ZIP-backed, schema version 1, and
optional, so older tablets ignore it safely until they support it. It remains a
single complete encrypted artifact; it is not the planned v2 individual-PDF
delivery design.

Department Fire Plans are currently published as one monolithic ZIP of every
active plan (`_artifact_fire_plans`), so a larger `PUBLICATION_ARTIFACT_MAX_BYTES`
is only a temporary compatibility ceiling, not the ~2,800-plan scale solution.
The target Fire Plan architecture is a signed, versioned authoritative department
manifest plus immutable, individually encrypted Fire Plan PDF artifacts: each
manifest generation lists every canonical Fire Plan with its immutable encrypted
artifact/version/hash/size, unchanged PDFs reuse existing artifacts, and only
changed/new PDFs produce new artifacts. Tablet sync fetches and verifies the
latest signed generation, compares with the locally active generation, reuses
identical local artifacts, downloads only missing/changed artifacts, verifies and
decrypts each required new artifact, then atomically activates the new generation
only after everything required is ready; documents absent from the new manifest
are deleted/gc'd only after activation, and any failed download/decrypt/import
retains the previous complete generation. Deterministic keys/nonces are never
derived for deduplication; deduplication reuses already-built immutable artifacts
when sanitized content is unchanged.

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
