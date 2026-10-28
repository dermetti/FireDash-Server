# Publication detail and lifecycle hardening

## Existing structure inspected before editing

All scope detail pages already used `templates/publications/scope_detail.html` with shared status/actions partials, a paginated history table, a conditional danger zone, and the shared HTMX lifecycle modal container. List/browser rows used their own contextual action dropdowns. The detail action partial disappeared entirely for unchanged current publications. Status presentation lacked an explicit ownership label and treated any current pointer as successfully deliverable. History rollback required a publication artifact path, while services exempted document-v2 publications from that requirement without validating their generation. The active-delete predecessor query could select incomplete historical generations.

The existing scoped Rebuild POST route and `request_rebuild()` service were retained. The service previously deferred enqueueing until commit, allowing stale requests to coalesce pending work outside the requesting transaction. File Library publication authorization, source advisory locks, pinning, quotas, revisions, temporary access, worker processing and retained-generation delivery were inspected before integration. Existing station publication authority was preserved; no new Station Administrator permissions were introduced.

## Implementation

- The shared detail page always renders the Bootstrap Publication actions card. Contextual actions remain distinct, and Rebuild uses an outline button and an accessible Bootstrap confirmation modal with a scoped POST form, CSRF token and `confirm_rebuild=yes`.
- Eligible current, failed and unpublished scopes can request a clean rebuild. Queued, staged, running or review-ready attempts disable Rebuild with an explanation. Services recheck eligibility under the scope/source locks and allocate through the existing worker queue within the transaction. Repeated requests do not rewrite or duplicate an attempt. Source errors are controlled publication failures. Published content remains authoritative until worker success.
- Scope status identifies System, Department or Station, retains active-operation polling, reports review state and warns about incompatible current document delivery. History is responsive, distinguishes delivery availability from stored READY flags and explains blocked historical rollback. Historical deletion remains independent of rollback eligibility.
- `publication_restore_blocked_reason()` centralizes scope, status, readiness and supported-schema policy. Presentation checks batch manifests, keys and references without reading PDF or ciphertext files. Direct rollback and predecessor selection run full generic generation validation under lifecycle locks before activation. Active deletion skips incompatible predecessors in descending version order and refuses deletion if none is safe. The review-publish path also rechecks status and document completeness after locking.
- Historical plan validation checks immutable frozen identities and metadata against encrypted references, without requiring the mutable accepted PDF to retain its old hash. New builds still resolve authoritative accepted records and validate their hashes. Empty signed/keyed generations remain valid. Generic artifact cryptographic metadata validation was strengthened to match readiness constraints. Malformed File Library source shapes now raise expected adapter errors.
- No migration, cryptographic primitive, tablet wire contract, endpoint or signed generation was changed.

## Changed files

| Path | Change |
| --- | --- |
| `apps/publications/restore.py` | New centralized restore policy and batched presentation loading |
| `apps/publications/services.py` | Atomic clean rebuild allocation, duplicate refusal, source errors, strict restore/predecessor/review validation |
| `apps/publications/views.py` | Confirmation enforcement, Rebuild view-model, shared history eligibility and delivery warnings |
| `apps/publications/state.py` | Correct System ownership label |
| `apps/publications/document_dataset_adapters.py` | Separate frozen historical metadata validation from accepted-source resolution |
| `apps/publications/document_v2.py` | Historical readiness option and explicit artifact cryptographic metadata checks |
| `apps/file_library/publication.py` | Controlled validation of malformed frozen source structures |
| `templates/publications/_scope_actions.html` | Persistent shared actions card and Bootstrap confirmation modal |
| `templates/publications/_scope_detail_status.html` | Ownership, delivery compatibility and review status |
| `templates/publications/scope_detail.html` | Responsive history, delivery availability and rollback explanations |
| `apps/publications/tests/test_publications_ui.py` | All production scopes plus test registry, confirmation/security/permissions, source failures and HTML structure |
| `apps/publications/tests/test_phase4a_lifecycle.py` | Concurrent rebuild and pending/running immutability regressions |
| `apps/publications/tests/test_fire_plan_v2_delivery.py` | Invalid historical generations across four datasets, no partial activation, valid empty restore and safe predecessor selection |
| `apps/file_library/tests/test_publication.py` | Existing permission regression now expects refusal of an already queued rebuild |
| `docs/publication-lifecycle-hardening.md` | This report and owner acceptance checklist |

No files were removed. Historical migrations were not edited.

## Validation

All commands use `.venv/Scripts/python.exe` with `DJANGO_SETTINGS_MODULE=config.settings.test`. PostgreSQL tests use the configured local disposable pytest database and isolated fixture artifact roots. Pytest uses `-p no:cacheprovider` and the configured scratch directory. No complete application suite was run.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_publications_ui.py apps/publications/tests/test_phase4a_lifecycle.py -q --tb=short
```

Result: **45 passed** (52.09 seconds).

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/file_library/tests/test_publication.py::test_publication_permissions_and_ui apps/file_library/tests/test_publication.py::test_frozen_metadata_reuse_replacement_and_retention apps/file_library/tests/test_publication.py::test_revision_race_and_failure_preserve_previous -q --tb=short
```

Result: **3 passed** (10.62 seconds).

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_fire_plan_v2_delivery.py -k 'historical_generation or restore_valid or v2_manifest_grant_and_document_delivery or malformed_stored_document_metadata or generic_fire_plan_worker_failure_preserves_current_publication or postgresql_ready_guard_rejects_incomplete_generation or authorization_precedes_matching_artifact_etag or nonready_grant_cannot_download' -q --tb=short
```

Result: **54 passed, 20 deselected** (139.12 seconds).

Final selected runs total **102 passed, zero failed, zero skipped**. Earlier subset runs overlap these results and are not added to this total.

```powershell
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe -m ruff check apps/publications/document_dataset_adapters.py apps/publications/document_v2.py apps/publications/restore.py apps/publications/services.py apps/publications/state.py apps/publications/views.py apps/publications/tests/test_publications_ui.py apps/publications/tests/test_phase4a_lifecycle.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/file_library/publication.py apps/file_library/tests/test_publication.py
.venv/Scripts/python.exe -m ruff format --check apps/publications/document_dataset_adapters.py apps/publications/document_v2.py apps/publications/restore.py apps/publications/services.py apps/publications/state.py apps/publications/views.py apps/publications/tests/test_publications_ui.py apps/publications/tests/test_phase4a_lifecycle.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/file_library/publication.py apps/file_library/tests/test_publication.py
git -c safe.directory=C:/Users/derme/Documents/FireDash-Server diff --check
```

Results: **0 Django issues; no migration changes detected; all Ruff checks passed; 11 Python files already formatted; diff check passed.** Rendered-template checks are included in the UI run, including duplicate IDs, nested forms, labels, targets, CSRF fields and shared section ordering.

During development, focused tests exposed missing history explanation markup, an absent Dangerous Goods source fixture, and an obsolete expectation that Rebuild coalesces pending File Library work. These were corrected. One accidental overlapping invocation against the reused local pytest database caused temporary test interference; both invocations were stopped or completed, the disposable test database was recreated, and subsequent runs were serialized. No live database was involved.

## Owner-controlled acceptance

After a separately reviewed deployment, using existing authorized administrator accounts:

1. Open System, Department and Station scope detail pages. Check consistent section order, scope titles, responsive history, active polling and confirmation modal behavior. A current unchanged scope should offer Rebuild.
2. Confirm one selected scope rebuild and check that only that scope queues a new version; the previous version remains current until success. While work is queued/running, verify the disabled explanation and rejection of stale duplicate requests. Verify unauthorized accounts cannot invoke the scoped POST.
3. Inspect historical Fire Plans v11-v13 if still present. They must show a compatibility/completeness explanation, no rollback control, and no direct or indirect activation path. Do not delete or rewrite these records or their ciphertext as part of this acceptance.
4. Validate restoration only with an owner-approved complete generic generation, including an empty generation if useful. Current deletion must be unavailable without a safe predecessor; any destructive acceptance action requires separate owner authorization.
5. Preserve the ACTIVE Fake-iPad installation, credentials, keys and history. This task requires no adoption, reset, live client verification or historical artifact cleanup.

No known unresolved test failures remain. Browser-level visual acceptance is owner controlled; local validation covers rendered HTML, forms, IDs, modal targets and existing HTMX responses. UI eligibility is deliberately a metadata-only preflight; transaction-locked service validation is authoritative and may refuse a stale or additionally invalid candidate.

No Git writes, deployment, SSH, live server requests, live migrations, server data changes, historical data/ciphertext cleanup or Fake-iPad state operations were performed.

## History query optimization (local follow-up)

The pre-change browser decoration selected every superseded publication in the
visible scopes with all publication columns, manifest/key joins and reference
prefetches, even for non-document datasets. History loaded the 25-row visible
page plus every superseded predecessor; detail rendering independently scanned
predecessors again. These reads deserialized snapshots, build/change summaries
and unnecessary cryptographic data. Both document checks searched the complete
manifest list for every reference.

The optimized implementation changes only presentation queries and membership
lookup, with no migrations or template changes:

- `restore.py` provides a narrow publication projection and registry-driven SQL
  `EXISTS` checks for non-document predecessors. Scope identity, ownership,
  supported schemas, READY state, usable artifact path and the current-version
  boundary remain required. No historical publication objects or JSON payloads
  are materialized for this existence check.
- Document predecessors exclude unsupported schemas, non-READY records, legacy
  publication artifact paths and missing manifests/keys in SQL. Remaining
  candidates are inspected in batches of at most eight per unresolved scope,
  newest first, using a window rank and version cursor. Inspection stops for a
  scope when its first eligible predecessor is found. A manifest's existence
  alone never establishes eligibility. Reference/artifact metadata is loaded
  in one narrow joined prefetch query per batch.
- `views.py` loads history display fields for the requested 25-row page only.
  Necessary document snapshots/manifests/keys/references are explicitly loaded
  in a bounded batch for relevant visible rows. Obsolete, failed, cancelled,
  rejected and ordinary non-document rows do not fetch snapshots. The detail
  view reuses its already-computed predecessor availability rather than running
  a second predecessor scan. UI actions, explanations and pagination remain.
- `state.py` includes `scope_type` in its existing narrow scope projection,
  removing a deferred-field query per displayed scope exposed by full rendering.
- `restore.py` and `document_v2.py` build validated artifact-ID maps, explicitly
  rejecting duplicate/non-string IDs and mismatched reference sets before
  matching metadata. All previous content, type and cryptographic metadata
  checks remain. Instrumented nonempty-generation tests verify exactly D
  artifact-ID reads for D documents in each validator (2 and 12 documents),
  instead of repeated D-by-D scans. The prefetched presentation check performs
  zero additional queries.

Transaction locks and authoritative service validation in rollback, predecessor
selection, active deletion and review publication are unchanged. Historical
validation still uses frozen metadata rather than mutable accepted PDF hashes.
Complete empty generations remain valid. No retention function or setting was
changed: retention protects the two newest READY superseded records, **not** the
two newest fully restorable generations. A protected legacy/incomplete READY
record can therefore still be rejected by restore policy. Changing that
retention policy requires separate owner review.

### Measurements

`test_history_queries.py` uses `CaptureQueriesContext` and instruments
`DatasetPublication.from_db` to count materialized publication objects and
snapshot fields. Snapshot byte totals are `len(json.dumps(snapshot))` over
materialized objects, not estimates from elapsed time or physical PostgreSQL
storage size. Each synthetic Hydrant snapshot is 131,090 serialized bytes.
Measurements use isolated PostgreSQL fixtures; maintenance has not retired any
of these histories.

| Query path / history | Before queries | After queries | Before objects | After objects | Before snapshot bytes | After snapshot bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Browser state + decoration, 1 scope | 7 | 7 | 3 | 2 | 4 | 0 |
| Browser state + decoration, 10 scopes | 7 | 7 | 30 | 20 | 40 | 0 |
| Browser state + decoration, 50 scopes | 7 | 7 | 150 | 100 | 200 | 0 |
| Hydrants: 2 predecessors, page 1 | 6 | 5 | 5 | 3 | 524,364 | 0 |
| Hydrants: 25 predecessors, page 1 | 6 | 5 | 50 | 25 | 6,423,414 | 0 |
| Hydrants: 200 predecessors, page 1 | 6 | 5 | 225 | 25 | 29,364,164 | 0 |
| Hydrants: 25 predecessors, page 2 | 6 | 5 | 26 | 1 | 3,408,340 | 0 |
| Hydrants: 200 predecessors, page 2 | 6 | 5 | 225 | 25 | 29,495,250 | 0 |

Browser baseline snapshots were NULL (four serialized bytes each); Hydrant
histories use the large synthetic payload. Both measurements were taken against
the original implementation before the query refactor. Full browser requests
initially exposed 27 / 36 / 76 queries at 1 / 10 / 50 scopes due to the deferred
`scope_type` field. The final rendered requests use **26 queries at every size**
and load zero snapshots, including during template rendering.

For document histories with 2 / 25 / 200 predecessors, including missing and
legacy generations plus obsolete rows, page 1 uses nine queries and loads only
three / two / two snapshots. At 200 predecessors page 2 uses eight queries and
loads one snapshot (the off-page complete empty predecessor). These snapshots
are 18-byte empty collections; the irrelevant 128 KiB payloads are never
materialized. A separate test traverses 18 malformed generic generations before
finding the valid nineteenth candidate, demonstrating that batching does not
impose an unsafe history cutoff. Three mixed SYSTEM / DEPARTMENT / STATION
scopes require two predecessor queries and zero publication objects.

Selected-column evidence: history and document base projections are explicitly
listed in `RESTORE_PRESENTATION_FIELDS` (identity, owner, version/schema, status,
readiness/path and timestamps). Tests assert no snapshot field is materialized,
no `build_summary`/`change_summary` columns are selected, and no document tables
are queried on ordinary non-document history/browser paths. State queries may
still evaluate `source_snapshot IS NOT NULL` as a boolean for Inspect changes;
they do not select or deserialize the JSON value. Rendered requests are also
instrumented to catch accidental deferred-field reads.

Trade-off: proving the absence of a restorable document predecessor can still
require inspecting every otherwise plausible generation if all are malformed.
Memory is bounded per batch, cheap incompatibilities are filtered in SQL, and
work stops after a valid predecessor is found. No arbitrary cutoff, persistent
eligibility flag, cross-request cache or weaker restore policy was introduced.

Changed files for this follow-up: `apps/publications/restore.py`,
`apps/publications/views.py`, `apps/publications/state.py`,
`apps/publications/document_v2.py`,
`apps/publications/tests/test_fire_plan_v2_delivery.py`, new
`apps/publications/tests/test_history_queries.py`, and this report.

### Focused verification commands

All commands below set `DJANGO_SETTINGS_MODULE=config.settings.test`. Tests use
only the configured local disposable PostgreSQL test database and isolated
fixture storage; no full-suite command or maintenance command was executed.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_history_queries.py apps/publications/tests/test_publications_ui.py apps/publications/tests/test_phase4a_lifecycle.py apps/publications/tests/test_retention.py -q --tb=short
```

Result: **70 passed** (75.28 seconds), covering pagination, rendered actions,
CSRF/reauthentication/authorization, scoped rebuild concurrency, non-document
rollback and unchanged retention behavior in addition to query measurements.

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_fire_plan_v2_delivery.py -k 'historical_generation or restore_valid or artifact_index or membership_index or malformed_stored_document_metadata' -q --tb=short
```

Result: **43 passed, 36 deselected** (114.46 seconds). Combined final regression
runs: **113 passed, zero failed, zero skipped**.

The selected cases cover all four document datasets, direct and indirect
activation refusal without partial effects, complete empty restores, historical
accepted-PDF replacement, typed metadata, duplicate/missing/non-string artifact
identities and counted linear membership lookup.

```powershell
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe -m ruff check apps/publications/restore.py apps/publications/views.py apps/publications/state.py apps/publications/document_v2.py apps/publications/tests/test_history_queries.py apps/publications/tests/test_fire_plan_v2_delivery.py
.venv/Scripts/python.exe -m ruff format --check apps/publications/restore.py apps/publications/views.py apps/publications/state.py apps/publications/document_v2.py apps/publications/tests/test_history_queries.py apps/publications/tests/test_fire_plan_v2_delivery.py
git -c safe.directory=C:/Users/derme/Documents/FireDash-Server diff --check
```

Static results: **0 Django issues; no migration changes detected; Ruff passed;
six Python files formatted; diff check passed.** No templates, lifecycle
services, retention implementation/settings, wire format or signed generations
were changed. No Git writes, deployment, live database access, server cleanup,
background job execution, signing-key/artifact operations outside disposable
fixtures, or ACTIVE Fake-iPad state operations were performed.

The measurement-only rerun used:

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_history_queries.py -q -s --tb=short
```

Result: **17 passed** (overlaps the 113 above, not added again). Rendered Hydrant
detail requests use **25 queries** on both pages at all three history sizes,
with zero snapshot fields fetched. No unresolved test failures remain.
