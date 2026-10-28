# Publication retention and artifact cleanup hardening

Local implementation, 28 October 2026. No server operations or activation.

## Pre-change analysis

Retention discovered excess SUPERSEDED publications with a PostgreSQL per-scope
row-number window. It protected the newest two READY/artifact_ready rows, even
when they had no usable document generation. Failed/Cancelled snapshots used a
30-day expiry, measured from job completion where available. Discovery selected
IDs; each mutation acquired scope, job, then publication locks, skipped live
jobs/current pointers and rechecked protection. The existing maintenance dry-run
avoided mutation but acquired these locks. It is not the new read-only audit.

Retirement changes status to OBSOLETE, clears source_snapshot, revokes grants
through the existing lifecycle helper, releases terminal document/File Library
content references, and schedules publication ciphertext removal after commit.
Historical publication identities, versions, timestamps, signed artifact
metadata and existing audit evidence remain. Generic artifact rows are locked;
physical deletion is queued only after the last reference disappears. The
DocumentArtifactCleanup queue survives failed filesystem deletion and retries
on subsequent maintenance. Historical untracked document ciphertext is not
inferred to be disposable from its age.

Previously remove_artifact_path used unlink(missing_ok=True), while callers
incremented a removal count even for absent files. READY paths intentionally
remain immutable, so daily scans revisited them and claimed the same removals.
The total also mixed temporary directories, document queue processing and orphan
artifact database rows with actual file deletion. No local logging parser for
the old terminal-artifact sentence was found; maintenance output tests have been
adapted to the explicitly named counters.

## Policy and default-off activation gate

`PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED` defaults to **False**. The existing
strict boolean environment parser accepts false/0/off as false; missing
configuration also means false. No environment file, systemd unit/timer or
migration activates this setting.

* False: the original READY-rank selection and retirement decisions are kept.
* True: protect up to `PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS` newest
  genuinely restorable SUPERSEDED versions, not merely READY rows. The default
  limit remains two. Invalid generations consume no protected slot. Zero or one
  valid predecessors are legitimate results.

The current authoritative publication and active attempts remain protected.
Failed/Cancelled snapshot expiry (30 days), maintenance batch size (100), audit
history and reference-release rules remain unchanged. A legitimate retirement
preserves its historical database row; it does not make the version restorable.
Previously protected invalid versions may become retirement candidates at the
**next maintenance execution after explicit activation**. There is no special
case for Fire Plans or versions v11?v13.

## Shared restore contract and bounded queries

`restore.iter_predecessor_assessments()` and
`select_restorable_predecessors(scope=..., limit=...)` are read-only helpers.
They use registry artifact_format/schema definitions and exact scope, dataset,
department and station ownership. Cheap metadata failures are rejected before
loading any document payload. JSON/GeoJSON candidates use only the narrow
RESTORE_PRESENTATION_FIELDS projection, never source_snapshot or summaries.

Document candidates are processed in newest-first keyset batches of eight.
Missing manifests/keys are rejected without loading snapshots or references.
Plausible generations load their frozen source, full generic manifest/key,
artifact references/artifacts and File Library content references in batches.
Strict validation reuses those fresh batch relations without per-row queries.
Only this assessor opts into the batch validation path; normal build/activation
callers still query persisted state. File Library frozen membership reuses its
explicitly prefetched content relations.

The decision calls publication_restore_blocked_reason(..., strict=True), using
the same historical validator as authoritative rollback. It preserves frozen
identities, exact membership, typed metadata, hash, signature/encryption and key
wrapping metadata checks, including valid complete empty generations. Mutable
accepted PDFs do not invalidate historical Fire Plan/KLGV generations. No new
independent document validator, persistent eligibility flag or candidate cutoff
was added. Existing linear artifact-ID membership indexes are unchanged.

**Restore eligibility is metadata-level validation, not a physical ciphertext
availability/integrity audit or a new cryptographic signature verification.**
No private KEK/signing keys are needed, no key is unwrapped, and no artifact file
is read by retention selection or the audit. This matches the authoritative
restore contract rather than silently imposing a different storage policy.

Selection stops at the configured number of valid predecessors. If many invalid
versions exist, proving the absence of another valid version can require scanning
all of them, but payload memory stays bounded per batch. Scope discovery streams
narrow scope rows; enabled maintenance returns at most the configured candidate
batch. Scopes are visited deterministically by ID. Audit JSON necessarily stores
its metadata-only report for serialization; it never retains entire generation
payloads in the report.

## Locking and concurrency

Discovery remains outside lifecycle transactions. Every candidate is rechecked
under the existing **scope ? job ? publication** lock order. Selection is repeated
while holding that scope lock before retirement. Concurrent activation, rollback,
publication completion and retention for that scope serialize on the scope row.
A newly current or newly protected predecessor is skipped. Live jobs cause a
skip; staged/building/review rows are never superseded retirement candidates.
No cross-scope mutation transaction was introduced. Direct/indirect rollback
checks remain authoritative regardless of the retention feature flag.

Reference release still locks artifact rows, removes only terminal references,
rechecks sharing, persists cleanup and deletes ciphertext after commit. Durable
queue callbacks and retry workers now serialize on the same queue row using
SELECT FOR UPDATE SKIP LOCKED. A busy queue row is deferred, not deleted by a
second worker. Artifact existence/path checks still prohibit removing live
stored generic ciphertext. Queue failures remain persisted and observable.

## Independent read-only audit

Owner-executed commands, after separate server authorization:

```text
python manage.py audit_publication_retention
python manage.py audit_publication_retention --json
```

The command does not call retention, cleanup, lifecycle services, workers or
mutation audit recording. It performs SELECTs without lifecycle write locks.
Tests execute it inside a PostgreSQL READ ONLY transaction, with unavailable
private-key paths, existing pending grants/jobs/cleanup work and a sentinel file.

For every scope it reports dataset/ownership identifiers, current version,
current READY-ranked protection, proposed strictly restorable protection,
candidate reasons, newly unprotected versions, potential retirement counts,
policy disagreement and active-job/attempt/current protection. The configured
flag and limit are included. Active-job blocking is reported separately from
potential retirements; those are not a promise to mutate during a concurrent
operation. Empty histories and all three ownership types are supported.

Illustrative output for the supplied diagnostic scenario, **not a server result**:

```text
Dataset: department_fire_plans | DEPARTMENT <scope-id>
Current: 14
CURRENT POLICY: [13, 12]
PROPOSED POLICY: []
  v13 NOT_RESTORABLE ? Incomplete document generation
  v12 NOT_RESTORABLE ? Incomplete document generation
  v11 NOT_RESTORABLE ? Incomplete document generation
PROPOSED CHANGES ? NOT EXECUTED: lose protection [13, 12]
  Protected: 0; potentially retired: 3
DRY RUN ? No changes executed.
```

Only stable IDs, versions, booleans and concise validator reasons are printed.
No manifest bodies, encrypted wrapping values, tokens, credentials or private
material appear. Results are deterministic for unchanged data. The audit does
not freeze the operational database: run during a quiet period and repeat if
concurrent lifecycle activity changes the inventory.

## Accurate cleanup accounting

remove_artifact_path now calls unlink without an existence-check race and
returns `deleted` or `missing`. Only FileNotFoundError means already missing;
permission/I/O/path errors remain errors. Canonical root containment remains,
symlink traversal is rejected, and housekeeping never removes the artifact root.
No additional historical ownership or age-based deletion rule was introduced.

CleanupResult replaces ambiguous integer totals in both cleanup helpers.
Maintenance includes actual retention post-commit deletion outcomes, terminal
artifact retries, generic durable queue work, known orphan records and the
pre-existing top-level orphan pass. It prints:

* files_deleted: ciphertext files physically unlinked successfully;
* files_already_missing: unlink attempts finding no file;
* cleanup_deferred: attempts left for retry, including skipped locked queue work;
* cleanup_failed: permission/I/O/path failures (also deferred);
* temporary_entries_removed: stale temporary directory trees removed successfully,
  separate from ciphertext file counts;
* document_cleanup_records_processed: durable queue rows successfully completed,
  whether the file was deleted or already absent;
* document_orphan_records_removed: unreferenced generic artifact database rows
  removed and durably queued, independent of physical deletion;
* document_cleanup_pending_records: actual outstanding durable queue depth at
  command completion, separate from per-pass attempt counters.

Counters describe attempts, not unique paths. A failed attempt followed by a
successful retry in the same pass retains evidence of that failure. A terminal
path removed during retention can subsequently be observed as absent by the
terminal scan; physical deletion is still counted exactly once. Post-commit
work inside an external transaction is reported as deferred until committed.
READY paths/signatures are not cleared to suppress statistics. Such paths may
still be cheaply rechecked daily; the repeated result is accurately labelled
already missing. Non-READY path clearing remains unchanged. Missing files are
not operational failures. No new persistence schema is needed.

The existing orphan summary and publication retention summary are retained;
the misleading terminal removal total is replaced with named metrics. Review
external journal parsers if they matched that old sentence exactly.

## Changed files

* config/settings/base.py ? explicit default-off boolean setting.
* apps/publications/restore.py ? shared bounded strict predecessor assessment.
* apps/publications/retention.py ? gated discovery and locked protection recheck,
  optional cleanup accounting propagated through existing retirement.
* apps/publications/document_v2.py ? opt-in fresh batch relation reuse.
* apps/file_library/publication.py ? batch-prefetched frozen content reuse.
* apps/publications/cleanup_results.py ? new explicit accounting result.
* apps/publications/artifacts.py ? deletion outcomes, safe retries and temp metrics.
* apps/publications/document_artifacts.py ? durable queue serialization/accounting.
* apps/publications/services.py ? optional post-commit outcome accounting only.
* apps/publications/management/commands/cleanup_orphan_artifacts.py ? accurate output.
* apps/publications/management/commands/audit_publication_retention.py ? new read-only audit.
* apps/publications/tests/test_retention_hardening.py ? new focused policy/audit/cleanup tests.
* apps/publications/tests/test_retention.py, test_artifacts.py, test_fire_plan_v2.py ?
  existing assertions adapted to explicit cleanup results.
* docs/publication-retention-cleanup-hardening.md ? this report.

No templates, endpoint contracts, cryptographic primitives, registry definitions,
retention limits, historical migrations or server environment files were changed.

## Owner-controlled activation

**Phase A ? Local implementation and validation:** disposable local PostgreSQL
fixtures only. No maintenance on existing runtime data, server access or
Fake-iPad operations.

**Phase B ? Deploy disabled:** owner reviews/deploys separately with the flag
absent or explicitly false. Existing READY-rank selection remains active;
accurate cleanup statistics become active. Keep the existing maintenance
schedule under owner control. No migration is required.

**Phase C ? Authorized read-only inventory:** owner separately authorizes and
runs the audit, saves its private output and reviews every affected scope,
invalid candidate and newly unprotected version. Review backups, exact storage
ownership and shared references. The provided v11?v14 scenario is diagnostic,
not approval to dispose of any version or ciphertext.

**Phase D ? Separate approval:** only after review, explicitly enable
PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=true through the normal owner-managed
configuration/restart procedure. The next scheduled retention pass can retire
previously protected invalid versions. To withdraw approval **before any such
pass**, keep maintenance from running, set the flag false and restart/reload the
relevant process configuration, then re-audit. Setting it false later changes
future selection only: it cannot recover snapshots, references or physically
removed ciphertext. Recovery after cleanup requires separately reviewed backup
restoration, not a feature-flag toggle. Never rewrite signed generations in place.

No Git writes, deployment, SSH/remote database access, live retention, server data
changes, key/credential operations or ACTIVE Fake-iPad changes were performed.


## Local performance evidence

All measurements below are from isolated PostgreSQL fixtures, not server data.
CaptureQueriesContext records actual SQL; tests inspect selected columns.

| Case | Measured queries | Payload evidence |
|---|---:|---|
| Strict selector, 25 Hydrant predecessors, limit two | 1 | No source_snapshot column selected, no document queries |
| 19 invalid document generations, zero valid | 6 | Missing manifest/key rows rejected before snapshot loading |
| 19 invalid generations plus one complete empty older generation | 8 | Batched manifest/key/reference/content loading |
| 19 invalid generations plus three complete empty older generations | 8 | Newest two selected; no arbitrary history cutoff |
| Browser requests, 1 / 10 / 50 scopes | 26 / 26 / 26 | Zero snapshot fields materialized |
| Hydrant detail requests, 2 / 25 / 200 historical rows, pages 1 and 2 | 25 each | Zero snapshot fields materialized |
| Hydrant history context, same sizes/pages | 5 each | Only visible page rows materialized |

The document selection counts hold for all four document datasets. The strict
selector intentionally does more work than the old metadata-only READY rank;
that additional validation is gated off by default. Existing browser/detail
measurements are unchanged from the preceding optimization. Existing counted
membership tests still read each artifact identity exactly once (D=2 and D=12),
not D squared. These are query/payload assertions, not elapsed-time estimates.

Cleanup fixture measurements: deleting one terminal ciphertext reports
files_deleted=1; the next pass reports files_deleted=0 and
files_already_missing=1, while preserving the READY signed path. Permission
failure reports zero deletions and one failed/deferred attempt. Retry succeeds.
Stage-A orphan row removal is counted separately; failed post-commit physical
deletion leaves a durable queue row, and a later retry physically deletes once.
Shared referenced ciphertext and unrelated historical paths survive the focused
regressions. Successful temporary directory cleanup increments only its own
counter.

## Exact focused verification

Each command uses `$env:DJANGO_SETTINGS_MODULE='config.settings.test'`, the
configured disposable local PostGIS test database and fixture storage. Pytest
cacheprovider is disabled per repository instructions.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_retention_hardening.py apps/publications/tests/test_retention.py apps/publications/tests/test_history_queries.py -q -s --tb=short
```

**50 passed in 90.20s.** Covers both flag modes, zero/one/three valid historical
generations across four document datasets, 19 newer invalid candidates, complete
empty generations, keyless/read-only/deterministic audit, pending grants/jobs,
mixed ownership, configured limits, current/active-job and newly protected
candidate races, immutable history, cleanup outcomes, snapshot expiry and all
prior browser/history query measurements.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db --create-db apps/publications/tests/test_fire_plan_v2_delivery.py -k 'historical_generation or restore_valid or membership_index or artifact_index' -q --tb=short -x
```

**34 passed, 45 deselected in 104.28s.** Includes direct rollback/active-delete
refusal for incomplete generations across all four datasets, no partial
activation, valid complete empty restores, accepted-PDF replacement independence,
and duplicate/missing/invalid artifact IDs and linear membership matching.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_artifacts.py apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_fire_plan_v2_delivery.py -k 'cleanup_removes_stale_temp or remove_artifact_path or generic_retention_preserves_shared or cleanup_rejects_unrelated_paths or reuse_rejects_noncanonical_path or v2_manifest_grant_and_document_delivery' -q --tb=short -x
```

**7 passed, 118 deselected in 10.63s.** Verifies shared artifacts, terminal release,
queue retries, canonical/unsafe paths, historical ciphertext preservation,
temporary cleanup and normal generic manifest/grant/download cryptography.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_retention_hardening.py -k 'nonready_terminal or stage_a_orphan' -q --tb=short -x
```

**3 passed, 25 deselected in 7.60s.** Additional non-READY present/absent cleanup
and Stage-A orphan/deferred physical deletion accounting.

Final non-overlapping regression total: **94 passed; zero failed or skipped.**
During development a regression run caught an unintended builder edit while
adding opt-in batch validation. That edit was removed, and the affected checks
passed afterward. The interrupted run left disposable fixture rows, so the
subsequent selected regression run recreated only the local pytest database
using --create-db. No runtime/server database was used. Earlier overlapping
successful development runs are not added to the total above.

```powershell
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe -m ruff check apps/file_library/publication.py apps/publications/artifacts.py apps/publications/document_artifacts.py apps/publications/document_v2.py apps/publications/services.py apps/publications/restore.py apps/publications/retention.py apps/publications/cleanup_results.py apps/publications/management/commands/cleanup_orphan_artifacts.py apps/publications/management/commands/audit_publication_retention.py apps/publications/tests/test_retention.py apps/publications/tests/test_artifacts.py apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_retention_hardening.py config/settings/base.py
.venv/Scripts/python.exe -m ruff format --check apps/file_library/publication.py apps/publications/artifacts.py apps/publications/document_artifacts.py apps/publications/document_v2.py apps/publications/services.py apps/publications/restore.py apps/publications/retention.py apps/publications/cleanup_results.py apps/publications/management/commands/cleanup_orphan_artifacts.py apps/publications/management/commands/audit_publication_retention.py apps/publications/tests/test_retention.py apps/publications/tests/test_artifacts.py apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_retention_hardening.py config/settings/base.py
git -c safe.directory=C:/Users/derme/Documents/FireDash-Server diff --check
```

Results: **zero Django issues; no migration changes detected; Ruff passed;
15 Python files already formatted; diff check passed.** No schema migration
was created or historical migration edited. No full test suite was run.

## Remaining operational considerations

* Metadata eligibility does not prove storage availability; review storage and
  backups separately before authorizing retirement.
* Very long invalid document histories can require many bounded inspections,
  including the mandatory locked recheck. Assess maintenance duration from the
  read-only inventory before activating; no unsafe cutoff was introduced.
* An audit without lifecycle locks is an inventory, not a transactionally frozen
  promise of the next maintenance result. Repeat after concurrent operations.
* Missing immutable terminal paths remain inexpensive retry candidates; no new
  schema tracks historical file absence. The reporting is now accurate.
* External log parsers matching the former terminal-removal sentence need owner
  review. Named counters and the existing retention summary are explicit.
* Historical untracked ciphertext still requires a separately authorized,
  ownership-proven inventory/disposition procedure. This change does not infer
  it is safe to delete and does not perform that work.
