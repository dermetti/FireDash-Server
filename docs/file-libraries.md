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

Quota policy changes acquire the same department capacity lock used by uploads but do not
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

## Projected capacity and administrative overrides

Valid sanitized PDFs are accepted into the canonical library immediately, subject
only to authorization, PDF validation, integrity and server quotas. They are
visible, previewable and downloadable before publication. Upload success is not
publication or delivery. Tablet storage approval never bypasses a server quota.

Capacity gates the actual frozen `department_files` or `station_files` candidate,
not an upload or replacement operation. The candidate's document set replaces
its own live-source set in the estimate, so new content is not counted twice.

Affected tablets are ACTIVE physical assets with current operational vehicle and
station assignments: all department tablets for `department_files`, and only the
current station for `station_files`. Inactive/lost/retired assets and unrelated
departments/stations are excluded. Missing current installations are capacity
unknown. An eligible current installation must be ACTIVE, have an unexpired lease,
and have a complete, nonfuture, valid measurement no older than
`TABLET_STORAGE_TELEMETRY_MAX_AGE_SECONDS` (default 86400). Missing, invalid,
outdated, replaced, or expired measurements never imply sufficient capacity.
An assignment beginning after the observation also means capacity unknown until
a fresh check-in: the earlier reading cannot establish the new scope's baseline.

The estimate uses the latest successful retained publication at or before the
measurement as an **expected published baseline**, not a cache receipt. It reserves
the unique ciphertext identities needed for the resulting department and current
station sources, successful generations published after the measurement, and
committed source-content changes since the measurement. `StorageImpact` is this
source-side reservation history; it is not a per-tablet delivery or measurement
series. Reservations survive source deletion and failed/superseded builds until a
newer measurement accounts for them. Duplicate logical-document/hash identities
are counted once. Existing generic artifact identities and actual ciphertext
sizes are reused; unbuilt content reserves sanitized size plus the 16-byte GCM
tag. Identical PDFs in different documents or scopes count separately. Replacement
and deletion never assume that old ciphertext has already been reclaimed. A
pruned successful snapshot needed since the measurement makes capacity unknown.

Projected free bytes = reported free bytes minus the estimated unique additional
ciphertext bytes; negative projections are preserved. A warning means projected
free bytes are **strictly below 20%** of total; exactly 20% is allowed. Estimates
exclude manifest/key metadata, decrypted working copies, other applications and
future disk activity. The published baseline may differ from an offline device's
actual cache. These are conservative estimates of incremental encrypted content,
never a guarantee of installability or confirmed delivery. The iPad must still
handle insufficient local capacity and failed staging without partial activation.

Source mutations in either scope serialize on a department advisory lock before
the established publication-scope/library locks. Capacity evaluation locks current
installation rows, so check-in/replacement cannot overwrite a reviewed measurement
mid-commit. Queries are batched, and estimating never builds a publication. Quota
updates use the department advisory lock followed by the quota-policy row lock;
publication workers retain the
existing frozen-snapshot, supersession and reference-protection lifecycle.

Unknown capacity requires department approval (warning); measured projected free
storage below 20% requires approval with a stronger danger warning. Mixed
populations show the stronger warning and retain all unknown diagnostics. No
eligible tablets is clear, not unknown.

`CapacityReview` retains the exact snapshot/revision, dataset and tenant scope,
projection fingerprint, tablet/assignment/installation identities, measurements,
decision actor and timestamps. Pending reviews have no upload retry deadline.
Department administrators must confirm and recently reauthenticate. Station
administrators can inspect their library's review but cannot decide. Temporary
station access confers no approval or publication rights. Approval accepts the
recorded publication risk, not a guarantee of installation or delivery; no
second upload is required.

Queueing/coalescing assesses the candidate; claiming rechecks and binds approval
to that generation; finalization rechecks before activation and consumes approval
atomically. An unapproved or stale BUILDING attempt (including one predating the
deployment) becomes obsolete and queues a fresh candidate. Current and retained
successful generations remain available. Failed encryption is still an ordinary
build failure; a capacity-blocked candidate stays pending, not failed. Edits
remain available and supersede reviews when their assessment context changes.
Repeated unchanged assessments reuse the same review without duplicate audit.
Failed or cancelled attempts supersede their unconsumed reviews. A recovered or
new attempt receives a fresh assessment and cannot inherit that authorization;
the accepted source PDF remains available throughout.

The department capacity lock precedes publication scope, library, review and
installation locks. Existing vehicle/tablet/assignment ordering is preserved.
Telemetry, installation, eligibility and source mutations schedule a committed
reassessment of pending candidates; a newly clear candidate or approved risk
wakes the existing worker. No build/encryption runs during a check-in. The worker
also rechecks clock-based expiry and recovers a missed refresh after restart.
Approval never transfers to another generation or source revision.

Migration `file_library.0010` adds nullable publication snapshot, generation and
supersession fields, permits automatic requesters and nonexpiring publication
reviews. Existing unconsumed upload reviews are marked superseded without
changing their decisions or immutable audit rows. They never authorize a
publication. Historically blocked uploads have no accepted source bytes to migrate;
those PDFs can be uploaded normally. Previously accepted PDFs need no second upload.
No content, quota usage, source revisions or jobs are rewritten.
Quiesce file writers and publication workers for the upgrade. Apply migration
`0010` before starting updated workers; an old worker process cannot enforce the
new gate. Existing queued and recoverable building attempts are reassessed by the
updated claim/finalization paths. Rollback requires quiescing writers
and workers and a compatible database restore: switching code alone would restore
the obsolete upload gate and cannot preserve this approval contract.

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
5. With unknown and measured below-20% storage, verify the source remains readable
   while publication waits. Approve as a recently reauthenticated department
   administrator and verify the queued candidate publishes without another upload.
   Refresh telemetry to safe values and verify a pending review clears automatically.

Stage 4 stores the latest device-reported storage measurement at authenticated
check-in and displays it in Tablet Management. An expired lease or stale
installation, or a measurement older than 24 hours by default, marks the
preserved values as last known capacity. Stage 5 adds the estimates described above.

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

The subsequent iPad PRD must cover mandatory integer storage check-ins and current
installation identity; explicit department/station scopes and both required schema-2
file datasets; configured root names and the display-only department ` (global)`
suffix; complete/generation Ed25519 verification, exact HPKE info, AES-KW and GCM;
individual ciphertext caching, hashes and immutable document identities; folder
integrity, authoritative empty generations and atomic complete-manifest activation;
lease/offline monotonic time, stale recovery, revocation/replacement/reassignment
and purge; and local capacity/staging failures without receipts or false delivery
claims. Server capacity overrides are portal-only and confer no tablet privileges.

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
The department parent includes paginated department-wide pending capacity
approvals and temporary station-access selection. Normal station administrators
see their library publication-capacity requests and a compact station publication panel.
Department and station file-publication administration and history live in
Publications. File scope details use the capacity-gated Build now action; they
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
rename, move, replace, publication and capacity override remain denied by services.
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

### Capacity review placement and manual publication controls

Department File Management is the single department-wide queue for pending file
capacity approvals, including station libraries. Rows identify the owning library,
source revision, strongest risk, affected/unknown/below-20% counts and review
status. Detailed assessments and decisions return to the relevant paginated queue
row. System Settings retains quotas and other configuration, without a parallel
capacity approval table.

Department Publications includes department and station file scopes. Department
administrators may administer station publication lifecycles without gaining
station File Explorer authoring privileges. Scope filters, counts and pagination
remain department-scoped. File rows lead to the scope detail for a fresh capacity
assessment rather than computing projections for every row in a list.

File-publication details expose **Build now** in Actions for an eligible staged
candidate, without a second Publish Now section. Capacity status links back to
File Management; the complete tablet assessment stays in its review workflow.
Station administrators retain Publish Now on their own File Management page,
but cannot use it while capacity approval is outstanding or a build is running.
Approved assessments are checked again against the actual candidate and device
context. Both manual services reject blocked requests independently of disabled
HTML controls; uploads and other source operations remain available. Existing
successful generations remain distributable while newer candidates await approval.
