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
- `FILE_LIBRARY_QUOTA_BYTES`: default 1 GiB, copied into newly created libraries.
  Department administrators configure their department and station-library
  quotas independently in System Settings.
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

Quota policy changes lock the same library row used by uploads but do not
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

Uploads and replacements with changed sanitized hashes pass a capacity guard before
canonical writes. Unchanged document/hash replacements and metadata edits do not
create new encrypted PDF bytes and do not require an override. Quotas remain a
separate mandatory check, including when an override is approved.

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
updates keep their existing library-row lock; publication workers retain the
existing frozen-snapshot, supersession and reference-protection lifecycle.

A blocked PDF creates a `CapacityReview` and audit event, **not** a document,
canonical content blob, quota usage, source revision, or publication intent.
Temporary ingestion files are removed. Bulk uploads report each PDF independently.
The review page shows scope, operation, all affected tablets, measurements and
timestamps, reserved bytes, projected free bytes, and warning/unknown reasons.

Department administrators review pending requests in System Settings. They must
explicitly confirm and recently reauthenticate (the existing reauthentication
policy) to approve or reject. A review expires **15 minutes after its creation**.
Approval binds the requesting actor, scope, operation kind, logical document UUID,
name/folder/replacement target, sanitized hash/size, source revisions, installation
and assignment identities, telemetry, and calculated projection. It does not
change telemetry, quotas, publication permissions, or subsequent operations.
The user retries the same PDF through the review page; the deployed sanitizer uses
deterministic IDs, and any changed sanitized result requires a fresh review.

Only the original requester, still normally authorized to manage the library,
can commit an approved operation. Source and telemetry are recomputed under locks;
a stale/expired/rejected/used approval fails closed even if the old page is open.
Successful commit consumes approval atomically with the source write, usage,
revision, dirty intent, reservation and audit. Failure rolls back consumption.
Audit records include request/decision/consumption, actor and decision maker,
operation, affected scope/tablet IDs, measurements/timestamps, projections/reasons,
fingerprint and expiry.

Station administrators **cannot** approve. Their safe escalation is: blocked
operation -> department administrator decision -> original station administrator
retries the specific PDF. Department approval gives no station browsing/upload,
replacement or publication authority. Six-hour temporary station access remains
read/download/delete only; an administrator must release it before approving or
using an override. Direct service and URL authorization enforce these boundaries.

Migration `file_library.0007` adds only source-capacity reservations and reviews.
Safe rollback requires quiescing file writers and resolving pending reviews. It
removes reservation history; obtain fresh measurements before re-enabling capacity
guards after a rollback/redeploy. The earlier over-quota rollback caveat still applies.

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
when selecting the same PDF twice for an override retry.

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
