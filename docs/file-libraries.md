# File libraries (Stages 1–5)

`apps.file_library` owns department and station source libraries. The existing
persisted authorization scopes apply; department roles do not inherit station
library access, including when a department administrator has a station assignment.
System administrators have no library access. Suspended resources fail closed.

Department administrators may activate one audited temporary station-library
grant from their department library after explicit confirmation and recent
reauthentication. The persisted grant is bound to the user and station and has
an absolute six-hour expiry. It permits browsing, search, preview, download,
and one successful recursive deletion only. Switching stations, explicit
release, expiry, loss of department authority, another department-library
mutation, or that successful deletion revokes the grant. Successful department
Settings, station/vehicle configuration, administrator lifecycle, and tablet
management mutations also revoke it at their domain-service boundary. Read-only,
invalid, and no-op operations preserve valid access. Expiry cleanup commits
outside the file mutation's rollback boundary, including when deletion is denied.
This capability is
separate from normal scope authorization, publication authorization, and tablet
installation entitlements.

Libraries are created lazily on first authorized access. A library has exactly one
department or station owner. `Entry` gives folders and logical documents a shared,
case-insensitive sibling namespace, including at root. UUIDs survive renames,
moves and PDF replacements. PostgreSQL constraints and triggers enforce scope,
immutable identities/content metadata, parent kind, cycles and five folder levels.

Every source mutation goes through the services and locks its publication scope
before its library row. The transaction covers canonical entries, current usage,
one revision increment, the audit event and the coalesced publication intent.
The scope revision is synchronized to the library revision. Bulk uploads are independent per-file transactions with individual
feedback. Recursive deletion is one mutation and its audit records every removed
identity. No-op edits do not advance revisions. Current usage counts each logical
document's sanitized content size; historical/unreferenced content is excluded.

## Storage and configuration

- `FILE_LIBRARY_MAX_FILE_BYTES`: default 25 MiB, enforced on both input and output.
  The existing `MAX_PDF_INPUT_BYTES` / `MAX_PDF_OUTPUT_BYTES` remain upper bounds.
- `FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES`: default 5,000,000,000 bytes (5 GB).
- `FILE_LIBRARY_STATION_QUOTA_BYTES`: default 2,000,000,000 bytes (2 GB per station).
  All stations in a department use its one shared station policy.
- `FILE_LIBRARY_QUOTA_BYTES` remains the historical default (1 GiB) for old
  migration callables. If explicitly configured, it is also the fallback for
  either new setting unless that setting is explicitly configured.
  Department administrators configure the department-files limit and shared
  per-station-library limit in System Settings.
- `FILE_LIBRARY_MAX_QUOTA_BYTES`: default 1 TiB, the server-side upper bound for
  administrator-configured quotas.
- Existing quarantine, broker, sanitizer-output and accepted-storage settings apply.
  Accepted keys are `file-libraries/<library UUID>/<content UUID>.pdf`, beneath the
  private `REFERENCE_DATA_ACCEPTED_ROOT`, never beneath static or public media.

Input and sanitized output undergo MIME, signature, strict PDF structure,
encryption and active-content validation. The existing Linux broker is mandatory;
there is no unsandboxed production fallback. Downloads/previews require live scope
authorization and verify size and SHA-256 before serving with private/no-store
headers. Client filenames never become storage paths.

Quota policy changes acquire the same department source lock used by uploads but do not
increment the source revision, queue publication work, or alter current usage.
A quota may be reduced below current usage. In that state, uploads and
replacements that increase usage are rejected; deletion, metadata changes, and
replacements that preserve or reduce usage remain available.

## Publication and retention

`department_files` and `station_files` are required, schema-2
`document-manifest-v2` datasets. The existing build worker discovers active
departments/stations lacking scopes, including libraries never opened in the
portal. Initial empty libraries publish normally. Existing Stage 1 revisions
are preserved when their publication scopes are initialized. Source edits queue
the normal next nightly build window; Publish Now expedites the same intent.
Neither UI state nor a published generation confirms tablet receipt.

The worker freezes the complete source tree under the publication scope lock,
then creates `PublicationContent` protected references before committing its
BUILDING transition. Source edits take that lock first, so a frozen tree cannot
combine revisions. In-progress builds and retained successful generations pin
immutable sanitized content independently of current entries. A source revision
change during encryption retires the candidate without activating it and queues
a fresh intent. Failures preserve the current successful generation.

The generic document adapter uses frozen logical UUIDs, metadata and pinned
content, never a current-document lookup. Unchanged document/hash pairs reuse
the existing per-document encrypted artifact. Library/folder/name changes still
produce new signed authoritative manifests. PostgreSQL guards validate scope,
frozen references, ready-generation completeness and immutable source/crypto
metadata. No monolithic archive is created. The wire contract is specified in
`docs/tablet-api.md`.

Normal publication retention keeps the current generation and the two most recent
successful predecessors (`PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS=2`). Terminal
unsuccessful attempts do not count. Retiring a generation releases its content
pins and standard encrypted-artifact references; the existing artifact cleanup
infrastructure handles final-reference removal. Historical file-library downloads
and grants remain usable while retained, but every request rechecks the current
installation/department/station entitlement. Station reassignment immediately
denies old station-library identifiers. Administrative scope checks also apply
to direct generic publication lifecycle URLs and services.

Replacements create new immutable `Content` objects. Deletion unlinks logical
documents immediately. Old content remains until explicit maintenance:

```
python manage.py collect_library_content --older-than-days 7
```

Collection locks content and uses Django's protected-reference deletion plus
database foreign keys; physical removal happens only after commit. Publication
content pins reference `Content` with `on_delete=PROTECT`. Current document
references use this same rule. Do not delete files independently of that lifecycle.
Content may be shared by multiple documents within a library; quota counts logical
current content, not unique physical bytes. Stage 1 does not deduplicate uploads.

## Accepted-storage reconciliation

Filesystem promotion still precedes database commit. Ordinary failures remove
newly promoted files; crashes, an enclosing transaction rollback, or failed
post-commit unlink can leave an orphan. Run the narrow reconciliation command
as the accepted-storage owner, alongside the existing maintenance jobs:

```
python manage.py reconcile_library_storage --older-than-days 7 --dry-run
python manage.py reconcile_library_storage --older-than-days 7
```

It scans only `file-libraries/<UUID>/<UUID>.pdf`, skips symlinks and noncanonical
paths, and keeps every file with a durable `Content` row, whether current,
unreferenced, building or retained. It requires at least one day's grace (seven
days by default), rechecks references while holding the same per-library
PostgreSQL transaction advisory lock as promotion, and is idempotent. This lock
also protects an uncommitted library/content insertion: cleanup waits for commit
or rollback, even if the library row itself is not yet visible. It safely handles
directories left by rolled-back library creation. Quarantine and sanitizer output
remain governed by their existing cleanup lifecycle. Unknown layouts are ignored.

Deploy the promotion lock and reconciliation code together; drain old upload
workers before the first cleanup pass. Schedule the command with accepted-root
write permissions, not by broadening the publication worker's read-only accepted
storage access. Retained plaintext and encrypted artifacts remain outside the
current-source quota, so physical disk capacity still needs ordinary monitoring.

## Device storage is managed locally

Tablet storage telemetry is currently disabled. The iPad app manages device
storage locally, checks available space before downloading and activating updates,
and preserves previously verified data when an update cannot fit. Offline lease
and authorization requirements still apply. The server receives no automatic
device-capacity measurements or derived insufficient-storage reports. No diagnostic
report API is provided by this change. Future MDM-based inventory or capacity
monitoring is a separate project.

Source-library quotas, department/station quota policies, PDF size restrictions,
source usage accounting, and server filesystem safeguards still apply.

Valid sanitized PDFs enter the canonical source library subject to authorization,
validation, integrity and source quotas. Authorized `department_files` and
`station_files` publications use the normal publication lifecycle without storage
projections, a 20% threshold, unknown-capacity warnings or department capacity
approval. Queueing, claiming, building, promotion and finalization do not consult
device capacity. Tablet check-ins and lifecycle changes do not reassess publications.

Source mutations and quota updates retain their department advisory lock before
scope/library locks. Frozen snapshots, source revisions and fingerprints, logical
clean-source reconciliation, explicit rebuild intent, job coalescing, encryption,
signatures, key grants, retention and artifact-reference protection remain in force.

Historical `AppInstallation` storage fields, `StorageImpact` rows and `CapacityReview`
rows remain in the database, with their constraints and audit history. Active
services neither write nor consult them. No schema/data cleanup migration is needed.
The telemetry freshness setting and review routes/UI have been removed.

Previously capacity-blocked PENDING jobs remain in the existing queue and become
claimable when their normal `not_before` schedule is due. Restart the publication
workers with the new code. Claiming reads current source under the source/scope
locks and freezes that source into the existing staged attempt. A source already
matching the active generation is reconciled without an unnecessary build; explicit
rebuild intent is retained. No deployment migration automatically queues unrelated
or unauthorized datasets. Normal retry/recovery and failure handling still apply.

## Deployment validation

Rolling back the Stage 4 quota constraint migration requires first resolving
any libraries whose current usage exceeds their quota.

Windows tests stub native MIME identification and the Linux sanitizer broker;
they exercise real pikepdf inspection and the real PostgreSQL/crypto lifecycle.
Before deployment, run a real Linux upload-to-publication check with libmagic,
the isolated broker, configured UID/GID boundaries and accepted-storage modes:

1. Upload a benign PDF through each role's library endpoint and verify sanitized
   bytes are readable by the publication worker but never publicly served.
2. Verify non-PDF, malformed, encrypted/password-protected and sanitizer-failure
   inputs fail without canonical entries, quota usage or publication mutations.
3. Publish through the worker, obtain a grant through an authorized test installation,
   and verify signed metadata, ciphertext hash and decrypted sanitized PDF hash.
4. Replace/delete source content, exercise retained-history cleanup, and run
   reconciliation with a deliberate aged orphan and an in-progress upload.
5. Check in with `{}` and no body; verify lease renewal, STALE recovery and
   unchanged app-version compatibility handling. Verify authorized department and
   station publications proceed without device measurements, and that source quota
   limits still reject storage-increasing writes when over quota.

No iPad browser is implemented. The future tablet client must
support these required types before activating a complete manifest containing
them, and render the department library with ` (global)` appended for display.
The suffix is never stored in the configurable name or signed source metadata.

### Exact Linux deployment verification (outstanding on Windows)

No real Linux/systemd/libmagic validation was performed on the Windows development
host. The PostgreSQL/cryptography tests run real database and encryption paths,
but stub native MIME and broker behavior. Run on the deployed Linux release:

```bash
sudo /srv/firedash/current/deploy/verify-deployment.sh
sudo systemd-run --unit=fire-library-sanitizer-smoke --wait --pipe --collect \
  --property=User=fire_backend --property=Group=fire_backend \
  --property=WorkingDirectory=/srv/firedash/current \
  --property=EnvironmentFile=/etc/fire-backend/fire-backend.env \
  --setenv=DJANGO_SETTINGS_MODULE=config.settings.production \
  /srv/firedash/current/venv/bin/python manage.py verify_library_pdf_sanitizer
```

The smoke command uses real native MIME/strict PDF inspection and the real isolated
broker for a benign PDF, rejects non-PDF/malformed/password-protected cases, and
cleans temporary files. It creates no library/document/publication records. A
missing broker, native dependency, or sandbox fails the command. Also complete the
four upload-to-publication checks above using authorized deployment test accounts;
the smoke command alone does not verify HTTP authorization, Nginx serving modes,
or publication-worker accepted-root access. Confirm deterministic sanitized hashes
across repeated sanitization of the same PDF; publication approval never requires
a second upload.

Reconciliation is appended to the existing daily
`fire-import-staging-maintenance.service` as `fire_backend`, the accepted-root
owner, with a seven-day grace period. The publication role keeps read-only source
access. Install the updated unit and verify the schedule explicitly:

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/fire-import-staging-maintenance.service /etc/systemd/system/fire-import-staging-maintenance.timer
sudo systemctl enable --now fire-import-staging-maintenance.timer
sudo systemctl start fire-import-staging-maintenance.service
sudo systemctl list-timers fire-import-staging-maintenance.timer --all
sudo journalctl -u fire-import-staging-maintenance.service -n 50 --no-pager
```

The unit's accepted-root write path is `/var/lib/fire-backend/fire-plans`; adjust
that narrow `ReadWritePaths` if deployment overrides `REFERENCE_DATA_ACCEPTED_ROOT`.
Do not grant the publication worker source write access. Inspect a dry run before
the first cleanup and drain pre-promotion-lock upload workers, as documented above.
Timer installation, execution, real in-progress-upload exclusion and sandbox/MIME
checks remain deployment prerequisites until verified on that host.

### Final server-to-iPad handoff inventory

The subsequent iPad PRD must cover storage-free check-ins and current
installation identity; explicit department/station scopes and both required schema-2
file datasets; configured root names and the display-only department ` (global)`
suffix; complete/generation Ed25519 verification, exact HPKE info, AES-KW and GCM;
individual ciphertext caching, hashes and immutable document identities; folder
integrity, authoritative empty generations and atomic complete-manifest activation;
lease/offline monotonic time, stale recovery, revocation/replacement/reassignment
and purge; and local capacity/staging failures without receipts or false delivery
claims. Device capacity never grants tablet privileges.

### Fake-tablet contract regression disposition

The two formerly failing optional/required-dataset tests exposed an obsolete
string-scope verifier and a fixture advertising KLGV as a ZIP. The verifier now
validates the server's explicit `SYSTEM`/`DEPARTMENT`/`STATION` scope objects against
current configuration, recognizes generic v2 descriptor URLs, and reconstructs
the current JSON artifact-signature scope (including `type`) separately from the
unchanged HPKE scope. The KLGV fixture now advertises the intentional schema-2
`document-manifest-v2` contract. Both tests still enforce their original intent:
an unsupported optional type is ignored after complete-manifest verification;
an unsupported required type prevents activation. Neither test is skipped or
removed. Negative cross-tenant/missing-ID scope and signature tests supplement them.

The fake tool remains a protocol harness, not the iPad PDF browser: recognizing a
v2 descriptor does not advertise support for activating required PDF libraries.
Publication API tests independently open real generic-v2 grants, decrypt individual
department/station PDFs, verify signed frozen metadata and hashes, and enforce
current entitlement for retained URLs. Performance fixtures now declare STATION
ownership explicitly and retain a fixed six-query bound including the existing
separate SYSTEM panel; no production constraint was relaxed to accommodate them.


## Department quota policies

Each department has exactly two source-storage limits, stored in bytes: one for
its department library, and one shared per-library limit for all its station
libraries, including future stations. These limits are independent across
departments and independent of tablet capacity. Settings accept decimal MB
(1,000,000 bytes) or GB (1,000,000,000 bytes), with exact whole-byte conversion
and the existing maximum bound. Quota changes neither revise sources nor publish.

Migration 0008 preserves the existing department-library quota. If existing
station quotas differ, it selects their **minimum**, reports the department UUID
and legacy limits in migration output, and applies that shared limit to every
station library. Review this output before activation: some libraries may become
over quota, but existing files remain intact and only growth is blocked. A scope
with no existing library uses the configured default. Reversing the migration
copies the current shared policies to legacy library columns; it cannot restore
the discarded per-station differences. Retain a database backup before migration
if restoration of the original individual limits is required.

Migration 0008 removes the legacy per-library quota column. Apply it with old
application processes quiesced; old code must not run against the new schema.
Returning to code that expects that column requires reversing 0008 first, in
addition to the normal deployment rollback prerequisites. This package does not
perform that migration or any release switch outside the local test database.

## Decimal default transition (migration 0009)

Migration 0008 is unchanged. Migration 0009 establishes separate callable
field defaults and evaluates each existing policy field independently. Only a
1,073,741,824-byte field with no explicit quota-change evidence is converted.
Recorded append-only audit installation must predate the initial file-library
migration, and migration 0008 must be recorded. Missing or inconsistent history,
unknown/unscoped quota audit records, explicit changes (including changing back
to 1 GiB), or custom default configuration preserve the affected values. Legacy
per-library audit targets protect the corresponding department or shared station
policy, including station limits reconciled by 0008. A malformed event scoped to
a department protects both fields; an unscoped event protects all policies.

This inference relies on the supported audited configuration paths. Direct SQL
writes are not identifiable from the available provenance. The migration locks
existing policy rows before reading audit evidence, so in-flight supported quota
changes commit before they are evaluated. It prints
updated and preserved counts for each field, never changes source usage/revisions,
publication jobs or audit rows, and does not backfill customized values. Its data
conversion is not reversed automatically: doing so could undo later administrator
intent. Schema rollback restores the old callable defaults only.

The Explorer read model lives in `apps.file_library.explorer`: each read
checks persisted library access, validates its folder using existing services,
and provides a bounded database-sorted page with complete containing-folder paths.
Search covers the authorized library recursively; sort keys are `name`, `modified`
and `size`, always folders first with UUID tie-breaking. Page sizes are 1 to 100;
invalid page numbers raise Django paginator errors. These operation hints never
replace service authorization. The shared Explorer UI consumes this read model; no independent folder-query implementation exists in its views.

`publications-department-files-publish` remains a POST-only department-admin compatibility endpoint
using the authoritative `publish_now` service. It
requires recent reauthentication and CSRF, coalesces existing jobs through the
normal publication queue, and redirects to department Publications (HTMX receives
`HX-Redirect`). The obsolete department File Management publish POST is denied; station publication remains on the station parent page.

## Shared File Management and File Explorer

`file-library` is one shared, compact parent page for department and station
administrators. It displays scope, storage usage and an Open File Explorer link.
The department parent includes temporary station-access selection. Normal station
administrators see a compact station publication panel.
Department and station file-publication administration and history live in
Publications. File scope details use the normal Build now action; they
do not render the compatibility Publish Now endpoint as an additional control.

`file-library-explorer` (`/files/<library UUID>/explorer/`) uses the same template,
read model, toolbar, rows and dialogs for both normal roles. Search covers the
whole authorized library and displays complete containing-folder paths. Sorting
and pagination execute in the database; pages default to 25 with a hard limit of
100. File operations remain authoritative service calls, preserving source
revisions, quota locks, sanitization, document identity, audit and publication
scheduling. Focused action GETs have standalone forms as well as HTMX dialogs;
POSTs recheck persisted scope and explicit deletion confirmation. GETs never
perform file mutations. Existing legacy file-mutation POSTs remain authorized
for compatibility; the old department publish action is explicitly denied.

Temporary station grants use these same pages in restricted mode, with scope,
expiry and release shown prominently. They allow browsing, search, preview,
download and one confirmed deletion. Successful deletion consumes the grant and
redirects to `/files/`; failed deletion preserves it. Upload, folder creation,
rename, move, replace, publication remain denied by services.
No department membership implies normal station management authority.

Station selection opens one reusable dialog with a server-filtered, keyboard-
and pointer-operated combobox. Results contain at most 20 active stations from
the authorized department; typing clears any prior UUID and confirmation.
Out-of-order suggestions are ignored. A standalone GET search and bounded native
select provide the no-JavaScript fallback (25 results plus a validated current
selection). Both modes POST through `TemporaryStationAccessForm`, preserving
recent reauthentication, six-hour expiry, grant replacement and audit. The chosen
station survives the reauthentication return; activation still requires a fresh,
explicit POST confirmation. Existing station sidebar and Station Data Hub routes
continue to enter the shared parent.

### Manual publication controls

Department Publications includes department and station file scopes. Department
administrators may administer station publication lifecycles without gaining
station File Explorer authoring privileges. Scope filters, counts and pagination
remain department-scoped. File rows lead to publication details, where **Build now**
is available for an eligible staged candidate. Station administrators retain
**Publish Now** on their own File Management page; running builds still disable
that control. Services enforce authorization and lifecycle checks independently
of HTML controls. No capacity review pages, queues, warnings or decisions remain.
