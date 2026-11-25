# Permanent beta baseline: Phase 2

## Approved forward extensions

The 16 accepted consolidated migrations and their historical evidence below remain
immutable. Phase I explicitly extends that baseline with
`tablets.0010_installation_credential_index`. Historical-schema tests target the
accepted nodes rather than current graph leaves: the baseline retains 296 indexes;
the current schema adds exactly the ordinary B-tree and `varchar_pattern_ops`
indexes on `tablets_appinstallation.credential_hash`. Tests separately verify the
historical schema, the forward upgrade, and fresh-current equivalence. The Phase 2
no-op statements below describe the original baseline finalization; an installation
at that historical baseline now has this approved forward migration pending.

Phase 1 commit `4ce840e376c5e0ac29f293f82f1168d3a27ab05a` is the accepted release. The user confirmed its successful Debian 13 / PostgreSQL 17 test-server deployment, all 16 replacement entries, removal of the hydrant preview table, successful deployment verification and readiness checks, and zero failed units. Those are acceptance facts supplied by the user; Phase 2 performs no server access or deployment.

## Files and permanent graph

The [exact retirement inventory](beta-phase2-retired-files.md) lists 104 deleted files: the original 103 plus the Phase 1 forward preview cleanup. The [old-to-new mapping](beta-baseline-mapping.json) remains historical documentation, also read by the accepted-release simulation test. Application migration execution does not read it.

All 16 approved consolidated migration filenames remain. Only their `replaces` assignments and following blank lines were removed. Operations, initial flags, dependencies, SQL, model definitions and initializers are unchanged. The [source fingerprints](beta-phase1-source-hashes.json) were derived from the accepted commit using read-only Git inspection, stripping only those assignments. A regression test verifies the retained sources against those fingerprints. The baseline was not regenerated.

The [complete final dependency graph](beta-phase2-graph.md) retains three publication stages and two tablet stages. All dependencies already referenced consolidated nodes in Phase 1, so none required retargeting. The graph is acyclic, has no replacement nodes or references to deleted files, and the first accounts migration creates the custom user model with swappable dependencies preserved.

## Installation behavior

Fresh beta installations execute the 16 ordinary application migrations using normal Django migration bookkeeping. Current registry, SYSTEM dangerous-goods scope, disabled mail singleton and Euro RESCUE singleton initialization remain deterministic, alias-aware and idempotent.

An established installation must already have the accepted Phase 1 state: all 16 consolidated migration names recorded. The permanent filenames are identical, so Django finds no pending schema work. Original migration recorder rows remain unchanged and harmless. Phase 2 introduces no database operations, conversion, pruning, fake flags, destructive migration, key changes or credential changes. A partially upgraded pre-Phase-1 database is outside this finalization's supported input state.

Current CapacityReview, StorageImpact, TabletReportDeliveryRequest, generic document generation and canonical hydrant/KLGV models remain. Retired preview, legacy Fire Plan delivery tables, tablet reactivation structures and quota columns remain absent. Existing records, persisted quota policies and cryptographic selectors are preserved.

## Test refactoring

| File | Change and coverage retained |
| --- | --- |
| `config/tests/test_migration_baseline.py` | Permanent graph/source checks, fresh schema/model columns, registry/singletons, exact Phase 1 catalog comparison, guards, initialization idempotence and established Phase 1 no-op upgrade. A test-only loader reconstructs accepted replacement metadata; normal Django migration execution creates all historical and replacement recorder entries. No recorder rows are inserted manually. |
| `tools/migration_catalog.py` | Removed HistoricalMigrationLoader and historical_executor. Retained test-only guarded disposable databases and catalog/dump capture. Dump normalization now ignores only comments, statement order and random PostgreSQL restriction tokens; names, column order, expressions and SQL bodies must match exactly. |
| `conftest.py` | Removed the historical-target disposable fixture and its imports; repository scratch-directory safety hooks remain. |
| `apps/publications/tests/test_document_migrations.py` | Tests the final cleanup queue and hardened generic functions/triggers directly; the obsolete queue rename fixture is gone. |
| `apps/publications/tests/test_scope_migration_upgrade.py` | Builds current READY department/station publications and retained legacy signature-upgrade markers. Preserves immutable artifact rejection, ciphertext preservation, lazy re-signing, marker consumption and repeat-call idempotence. |
| `apps/reference_data/tests/test_hydrant_location_migration.py` | Current PostGIS hydrant geometry versus descriptive location, canonical publication geometry, tenant-local unique hydrant identity and KLGV metadata/geometry round trip. |
| `apps/ingestion/tests/test_dangerous_goods_migration.py` | Current SYSTEM registration, rejection of tenant scope by model validation and invalid owner shape by database constraints. Removed fixtures for retired dangerous-goods publication/audit/import deletion. |
| `apps/tablets/tests/test_storage_migration.py` | Current storage columns, completeness/size checks, lifecycle choices and absence of reactivation schema. Existing API and lifecycle negative tests remain. |
| `apps/file_library/tests/test_libraries.py` | Removed only the historical forward target test; final installation and all guards are covered by baseline tests. Existing library behavior and tenant/concurrency tests remain. |
| `apps/file_library/tests/test_backend_preparation.py` | Removed tests exclusively for retired decimal-quota conversion, ambiguous audit inference and conversion/writer locking. Current quota defaults, persisted policies, authorization, enforcement and application regressions remain. |
| `apps/file_library/tests/test_management_ux.py` | Removed only obsolete per-library quota reconciliation/backfill test; management and audit coverage remains. |
| `apps/file_library/tests/test_publication_reverts.py` | Removed only historical 0029 backfill test; current explicit rebuild intent, reconciliation and publication behavior remain. |
| `apps/file_library/tests/test_review_presentation.py` | Reconciliation regression uses an existing clean bulk job directly instead of importing retired 0030. Assertions still protect cancellation and the active publication. |
| `apps/publications/tests/test_bulk_intent_migration.py` (deleted) | Exclusively tested retired 0029/0030 correction and audit inference. Current explicit intent/rebuild tests remain, and Phase 1 simulation verifies no conversion replays. |
| `apps/tablets/tests/test_stage22_migration.py` (deleted) | Exclusively tested obsolete REMOVED-row deletion. Current database rejection, installation recovery and cross-tenant lifecycle tests remain. |

The empty/nonempty preview cleanup tests retire with the already accepted cleanup migration. The table must be absent in both fresh and accepted established Phase 1 states; no cleanup is performed in Phase 2.

## Validation and evidence

Local validation uses only `config.settings.test` and disposable PostGIS databases. The available service remains PostgreSQL 16.15. Docker has no running engine and `pg_dump` is unavailable. Therefore no Phase 2 PostgreSQL 17 or schema-dump result is claimed locally. The accepted server verification establishes Phase 1 acceptance, not a new Phase 2 validation run.

The fresh comparison reconstructs accepted Phase 1 metadata from its historical mapping and pinned unchanged migration bodies, then compares a separately migrated permanent database. It checks all current model tables/columns, absent obsolete tables, registry/singletons, autodetector and migrate checks. Catalog comparison requires exact columns, constraints, indexes, final functions/triggers and table/sequence ownership/ACLs. Reference counts remain 679 columns, 341 constraints, 296 indexes, 16 protected functions, 20 triggers and 89 ownership entries; protected function hashes are also checked against the independent Phase 1 original-history reference.

The established simulation normally migrates through the accepted replacement graph, creates representative users, organizations, publication/cleanup records, immutable audit history, custom quota policies, operator singleton settings and cryptographic selectors, then switches to the permanent graph. It asserts a consistent history, an empty migration plan on both runs, no CREATE/ALTER/DROP/INSERT/UPDATE/DELETE/TRUNCATE statements, identical complete synthetic-record fingerprints, identical catalogs and unchanged recorder entries, including all 104 old names and 16 consolidated names.

Final focused runs: **145 passed, 1 skipped** across two selections (104 passed/1 skipped in 160.65 seconds, then 41 passed in 25.49 seconds). The skip is the unavailable schema-only `pg_dump` comparison. The selections include every refactored retained test module, file-library management/authorization/concurrency behavior, PostgreSQL generation completeness and crypto/membership immutability, registry scope guards, negative tablet telemetry and lifecycle checks, current quota concurrency/enforcement, explicit rebuild intent and canonical KLGV generation. No full local Python suite was run.

Both selections used `.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider --tb=short`. The first selected baseline/document/scope/hydrant/dangerous-goods/storage migration tests, file-library backend_preparation/management_ux/libraries/review_presentation, system_scope, the three PostgreSQL generation guard regressions in fire_plan_v2_delivery, and the phase6 registry scope trigger regression. The second selected tablet API invalid/preserved telemetry, stage22_lifecycle, stage4_quotas, explicit rebuild/coalescing regressions in publication_reverts, canonical metadata/immutable-PDF generation in klgv_manifest and the retained lazy signature regression.

Static results:

- Ruff check passes; Ruff format check passes for all 444 remaining Python files.
- Django system check and `makemigrations --check --dry-run` pass with test settings; fresh disposable `migrate --check` passes in the baseline test.
- Targeted mypy passes for all 13 changed retained Python files.
- Full mypy checks 413 files and reports only four pre-existing Windows-platform errors in unchanged Linux deployment code: `grp.getgrnam` in `deploy/lib/configuration.py:177`, and `os.chown`/`os.mkfifo` in `deploy/tests/test_credential_metadata_linux.py:140,144,165`. The Linux CI type-check result remains pending.
- Configured Bandit application scan passes with no findings (existing nosec/comment parsing warnings only).
- `git diff --check` passes; read-only Git inspection confirms no unrelated application/model/role/configuration changes. All 16 migration diffs remove replacement metadata only.

Earlier fixture development failures were corrected and the affected coverage passed in the final selections. Initial fixture assumptions about deep-copying Django swappable dependencies and registry-vs-owner-shape validation were corrected without changing the accepted baseline SQL or application behavior.

## Remaining gates

The existing GitHub CI server-tests job uses `postgis/postgis:17-3.5`, runs the full suite and supplies its PostgreSQL client to the normalized schema-dump comparison. Run that workflow against these changes after review through the separately authorized Git workflow. This session cannot validate uncommitted local code in GitHub CI without publishing it; staging, commits, pushes and pull requests are prohibited. No workflow against unrelated existing code was substituted for this validation.

There are no intentional Phase 2 schema differences. PostgreSQL 17 catalog/dump validation and the full Linux CI result remain outstanding until that environment runs these changes. No deployment was performed or requested by this implementation.
