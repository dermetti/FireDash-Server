# File libraries

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
- `FILE_LIBRARY_QUOTA_BYTES` is a temporary internal 1 GiB constant for historical
  migration replay only; it is no longer read from the environment.
  Department/station defaults do not rewrite persisted department quota policies.
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
portal. Initial empty libraries publish normally. Existing source revisions
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

Publication retention always protects the current generation. With default
`PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=False`, it protects the newest two
superseded READY-metadata rows (`PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS=2`),
even if strict restore validation would reject them. With separately approved
`True`, only genuinely restorable predecessors consume protected slots; fewer
than two can qualify. Terminal unsuccessful attempts do not count. Enabling
the flag can retire previously protected invalid generations at the next
maintenance run. Disabling it cannot recover removed ciphertext or references.
Review the [read-only audit and backups](operations.md#publication-retention-policy-and-audit)
before separate owner approval; repository defaults do not prove deployed values. Retiring a generation releases its content
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
current content, not unique physical bytes. Uploads are not deduplicated.

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

No iPad browser is implemented. The tablet client must
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

The iPad client must cover storage-free check-ins and current
installation identity; explicit department/station scopes and both required schema-2
file datasets; configured root names and the display-only department ` (global)`
suffix; complete/generation Ed25519 verification, exact HPKE info, AES-KW and GCM;
individual ciphertext caching, hashes and immutable document identities; folder
integrity, authoritative empty generations and atomic complete-manifest activation;
lease/offline monotonic time, stale recovery, revocation/replacement/reassignment
and purge; and local capacity/staging failures without receipts or false delivery
claims. Device capacity never grants tablet privileges.

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

`publications-department-files-publish` remains a POST-only Department-admin
compatibility endpoint using `publish_now`, recent reauthentication and CSRF.
It coalesces normal jobs and redirects to Publications (`HX-Redirect` for HTMX).
It is not an extra button on scope detail. Legacy authorized file-mutation POSTs
on the parent remain supported; the obsolete department publish action is denied.

## Shared File Management and File Explorer

`file-library` is one shared, compact parent page for department and station
administrators. It displays scope, storage usage and an Open File Explorer link.
The department parent includes temporary station-access selection. Normal station
administrators see a compact station publication panel.
Department and station file-publication administration and history live in
Publications. File scope details use the normal Build now action; they
do not render the compatibility Publish Now endpoint as an additional control.

The shared Explorer, route and permission details are specified
[below](#shared-explorer-routes-and-permissions). The department station selector
has keyboard/pointer behavior, stale-result protection and a no-JavaScript search
with up to 25 options plus the validated current selection. Selection survives
reauthentication return, but activation still requires a fresh explicit POST.

### Manual publication controls

Department Publications includes department and station file scopes. Department
administrators may administer station publication lifecycles without gaining
station File Explorer authoring privileges. Scope filters, counts and pagination
remain department-scoped. File rows lead to publication details, where **Build now**
is available for an eligible staged candidate. Station administrators retain
**Publish Now** on their own File Management page; running builds still disable
that control. Services enforce authorization and lifecycle checks independently
of HTML controls. No capacity review pages, queues, warnings or decisions remain.

## Logical reverts and clean rebuild intent

Logical comparison ignores source revision counters and internal content-row
IDs, while stable library/folder/document identities, names, hierarchy, sanitized
hashes and sizes remain significant. Frozen snapshots retain content IDs for
pinning and worker checks. Only a match with the active publication is redundant;
a match with superseded history requires a new generation and tablet update.
Unchanged document/hash pairs may reuse ciphertext without reusing generation
identity. Missing/incomplete historical snapshots fail conservatively dirty.
No immutable publication fingerprint backfill is required.

Explicit confirmed Rebuild persists `allow_clean_rebuild=True`; ordinary bulk
requests and Build Now do not acquire that intent. Timeout recovery and replacement
of changed-source attempts preserve it. Running builds finish/recover normally;
terminal obsolete attempts require a new explicit Rebuild. See the
[reconciliation runbook](operations.md#file-publication-logical-revert-reconciliation)
for read-only inspection and separately authorized repair.

## Shared Explorer routes and permissions

| Route | Purpose |
| --- | --- |
| `GET /files/` | Authorized-library dispatcher: redirect for one library, picker for multiple. |
| `GET /files/<library_id>/` | Scope/usage, library-name management, Open Explorer and role-specific controls. |
| `GET /files/<library_id>/explorer/` | Shared folder/search/list view in all three access modes. |
| `GET/POST /files/<library_id>/explorer/actions/<action>/` | Action form or scoped mutation: `upload`, `new-folder`, `rename`, `move`, `replace`, `delete`. |
| `GET /files/<library_id>/<entry_id>/pdf/` | Authorized preview; `?download=1` downloads the validated PDF. |
| `GET /files/<library_id>/station-access/` | Department-library temporary station chooser, including no-JavaScript fallback. |
| `GET /files/station-search/<department_id>/` | Authorized active-station name/short-code search, at most 20 results. |
| `POST /files/station-access/activate/` | Validated station selection, confirmation and recent reauthentication. |
| `POST /files/station-access/<station_id>/activate/` | Explicit scoped grant activation. |
| `POST /files/station-access/release/` | Release grant. |
| `GET /stations/<station_id>/data/` | Station Data Hub entry point. |

The shared `explorer.html` and read model serve normal Department admins, normal
Station admins and temporary Department-admin station access. Normal roles can
manage their authorized library; temporary mode permits only browsing, PDF
preview/download and one successful recursive deletion. Department membership
or a station assignment never substitutes for the service's persisted access
mode. System/unrelated administrators gain no source access. Department
Publications manages department files and authorized station publication scopes;
Station admins publish only from their own parent page. Temporary grants cannot
publish, and legacy department `action=publish` on File Management is denied.

Every list, dialog GET, mutation POST, preview and download independently checks
current user/library/entry scope. Posted UUIDs and display operation hints are
untrusted. Denials must not reveal foreign names or paths. Mutation POSTs require
CSRF; GET action forms do not mutate files. Malformed nonempty folder IDs never
silently select root. Grant-expiry revocation commits even if the requested
mutation rolls back. A consumed grant redirects to an authorized destination.

Explorer query parameters are `folder`, `q`, `sort` (`name`, `modified`, `size`),
`direction` (`asc`, `desc`), `page`, and `page_size` (default 25, range 1-100).
Search is trimmed/capped at 255 characters and spans the entire library; ordinary
browsing lists direct children. Results are folder-first with UUID tie-breaking;
search results include ancestor paths. Invalid folder/page values fail explicitly.
Pagination preserves search/sort/folder; new searches reset page. Move dialogs
browse a bounded page of destinations, never all folders per row. Services reject
cross-library moves, cycles, excessive depth, duplicate sibling names and unsafe
paths. Replacement preserves document UUID and name with new immutable content.

HTMX enhances real navigation/form URLs. Without JavaScript, standalone action
forms and station search/select remain usable. Errors remain visible without
closing/replacing the selected action; upload results remain per-file. Recursive
delete requires confirmation and warns about grant consumption. Keep labels,
keyboard access, focus return, current breadcrumbs, live selection/error messages
and non-color status text; see [UI conventions](FireDash_UI_Styleguide.md#file-management-and-file-explorer).
