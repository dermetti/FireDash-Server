# Beta migration consolidation, phase 1

Local implementation only. PostgreSQL 17 validation and GitHub CI results remain outstanding: the available test service is PostgreSQL 16.15. No deployment or Git mutation was performed.

## Replacement mapping and dependencies

All 103 original migration files remain unchanged. The normal forward preview cleanup is the 104th replaced migration. Sixteen replacements span thirteen applications. The [exact mapping](beta-baseline-mapping.json) names every original migration, including numbering gaps; the [dependency tables](beta-baseline-graph.md) give all declared dependencies. Custom-user references retain Django swappable dependencies, and the first accounts replacement creates User.

The tablet boundary preserves the original ordering: tablets 0002 precedes publications 0011, which precedes tablet cleanup 0006–0009. Collapsing both applications into one replacement each would introduce a cycle. Publications 0028 belongs to the middle publication stage, followed by the file-library replacement (including 0003 and 0010), then the final publication stage (0031–0033). The final stage creates DocumentArtifactCleanup directly under its current name.

Fresh installation executes replacement operations. Django records both the replaced names and replacement entries through its normal bookkeeping. An established database through the original 103 migrations temporarily uses reference_data's original path, executes only 0015, and then records its replacement. A database already through 0015 has no schema work pending. No custom tracking, fake flags, prune, or manual recorder edits are used. The supported established state is the complete Installer A history, with or without preview cleanup; arbitrary incomplete historical installations are not claimed as validated.

## Explicit final SQL retained

Final definitions were captured from the fully migrated disposable legacy reference, including the new cleanup. They are literal RunSQL operations and never obtain or patch an obsolete function at fresh installation. The [reference evidence](beta-postgresql16-reference.json) contains function fingerprints and all twenty enabled trigger definitions. The requested PostgreSQL 17 verification remains outstanding.

| Installation stage | Final functions |
| --- | --- |
| Audit baseline | `reject_audit_event_mutation` |
| Middle publication stage | `publications_validate_dataset_scope`, `publications_guard_artifact`, `publications_guard_phase7_closeout`, `publications_prevent_activation_mutation` |
| File-library baseline | `file_library_entry_guard`, `file_library_content_guard`, `file_library_scope_guard`, `file_library_guard_publication_content`, `file_library_guard_generation`, `file_library_guard_artifact_reference`, `file_library_guard_crypto_metadata` |
| Final publication stage | `publications_document_generation_complete`, `publications_guard_document_generation`, `publications_guard_document_reference`, `publications_guard_document_crypto_delete` |

The completeness function includes all 0033 hardening: canonical Fire Plan snapshot matching including path, numeric version/ciphertext-size JSON checks, and string generation_wrapped_cek validation. Audit protection denies UPDATE, DELETE and TRUNCATE, revokes runtime mutation privileges and retains SELECT/INSERT. File-library protections retain hierarchy, scope, immutable content, publication-content completeness, generation/reference integrity and final crypto immutability. Publication protections retain registry-backed scope validation, artifact readiness and safe paths, immutable activations, generic generation completeness, reference guards and crypto deletion restrictions. Production bootstrap, ownership and hardening scripts are unchanged.

## Required initialization

Initializers use historical state models, the migration connection alias and get_or_create. Repetition preserves operator values. Complete recorded original ranges prevent the replacement initializers from running again.

| Registration | Scope | Current / supported schema versions | Required | Feature |
| --- | --- | --- | --- | --- |
| department_fire_plans | department | 2 / [2] | yes | publications |
| department_klgv_plans | department | 2 / [2] | yes | klgv_plans |
| department_files | department | 2 / [2] | yes | publications |
| station_files | station | 2 / [2] | yes | publications |
| department_hydrants | department | 1 / [1] | yes | publications |
| station_personnel | station | 1 / [1] | yes | publications |
| department_phonebook | department | 1 / [1] | no | publications |
| station_phonebook | station | 1 / [1] | no | publications |
| dangerous_goods | system | 1 / [1] | yes | publications |
| test_department_incidents | department | 1 / [1] | no | publications |

Also retained: the SYSTEM dangerous_goods DatasetScopeState row; disabled SystemMailConfiguration; VehicleRescueGuidesConfiguration with Euro RESCUE, web mode and https://rescue.euroncap.com/. The runtime-only test_system_dataset is absent from both historical migration seeds and the baseline. The historical incident test registration remains.

## Schema intentionally eliminated and upgrade-only work omitted

The fresh path creates current models with final field types, defaults, relationships, constraints, named indexes and PostGIS geometry(Point,4326) columns. It omits retired Fire Plan artifact/reference/generation/key/grant/manifest tables, obsolete tablet reactivation fields, removed station address and historical quota columns, and HydrantImportPreview. CapacityReview, StorageImpact, TabletReportDeliveryRequest, current generic document-generation models and DocumentArtifactCleanup remain.

Fresh installation omits tablet version canonicalization/deletion/reactivation cleanup; station/address and canonical ingestion conversions; Fire Plan/KLGV metadata and identity backfills; historical publication lifecycle/source/format transitions; signed-manifest cutovers and cleanup; source revision backfills; old job/bulk-intent corrections; dangerous-goods publication/audit/import deletion; quota reconciliation/audit/backfill; upload-review retirement; and intermediate registry promotions. Only final initialization is performed. Retained historical files continue to implement their original upgrades where required.

## Preview cleanup behavior

reference_data.0015 takes ACCESS EXCLUSIVE on reference_data_hydrantimportpreview inside its atomic migration, counts rows through the migration alias, and refuses populated tables with only the table name and count. An empty table is removed with DROP TABLE ... RESTRICT. SeparateDatabaseAndState/DeleteModel updates Django state. Unexpected dependents prevent removal. The lock prevents a concurrent writer racing the emptiness check; the restrictive drop prevents cascading removal. The fresh baseline never creates this table. The canonical ingestion importer and its 50,000-feature limit are unchanged.

## Validation evidence and intentional physical differences

PostgreSQL 16.15 catalog comparison covers 679 columns, 341 constraints, 296 indexes, 16 protected functions, 20 triggers and 89 table/sequence ownership entries. Column definitions, function definitions, trigger definitions/enabled state and ownership/ACL entries match exactly. Constraint and index definitions match semantically. Lifecycle CHECK equivalence is evaluated across every combination of ACTIVE/SUSPENDED/REVOKED/invalid/NULL status and present/absent revocation timestamp/actor.

Intentional physical differences:

- Fresh column order follows current model declarations; historical ALTER operations leave a different order and dropped attribute slots.
- Seven implicit index names retain old names in the historical schema: four DocumentArtifactCleanup primary/unique/pattern indexes, the Hydrant geometry GiST index and two KLGV path unique/pattern indexes. Four associated PK/unique constraint names also inherit the old table/column names. Explicit model-defined names remain unchanged.
- Two authorization lifecycle CHECK expressions reorder equivalent AND operands.

Comparison preserves index/constraint semantics, multiplicity, uniqueness, deferrability and trigger enabled state. The normalized pg_dump comparison accounts for these differences and is wired into the existing PostgreSQL 17 CI service using that service's client. It skips locally because no pg_dump executable is available.

Established-server simulations fingerprint complete synthetic records before and after migration, including audit/publication records, quota policies, operator singleton values, cleanup queue entries and cryptographic selectors. Tests check both pending/already-completed cleanup, exact plans, absence of CREATE/ALTER, replacement entries, consistent history and a no-op rerun. Cleanup failure tests assert unchanged schema and records for populated previews and an unexpected dependent view. Fresh tests exercise audit mutation/TRUNCATE denial, cross-tenant scope rejection, content/activation immutability and publication closeout rejection. Initializer tests verify repeated calls preserve operator values.

Historical migration tests explicitly use an original-only loader on disposable databases. They continue to exercise the retained upgrade logic, without requesting replaced graph nodes or reversing a consolidated baseline. Telemetry and dangerous-goods migration tests now start from forward historical fixtures.

Focused tests: **106 passed, 2 skipped** across two selections. The retained migration/application selection passed 70 tests and skipped one test requiring native Unix group support (159.91 seconds). The transition/catalog/seed/cleanup, quota-migration and SQL document-generation integrity selection passed 36 tests and skipped the unavailable local pg_dump comparison (192.60 seconds). The 50,000-feature acceptance and 50,001-feature rejection tests passed.

The first selection covered publication document/scope/bulk-intent upgrades, system scopes and artifacts, dangerous-goods upgrades, hydrant import limits/location upgrades, tablet deletion/telemetry upgrades, library integrity and quotas. The second covered all new baseline tests, both quota backfills, incomplete generation closeout rejection, generic crypto/reference immutability, twenty historical document corruption cases across all four document datasets, and registry/station-ownership enforcement.

Ruff lint and repository formatting checks pass (550 files); Django system checks and makemigrations --check --dry-run pass. migrate --check passes on the disposable fresh baseline. Targeted mypy for the new validation helper/tests and root fixture passes. The configured application Bandit scan passes with zero findings. An additional scan including tests/test utilities flags assertions, synthetic SQL and subprocess use, which are outside the configured application scan. git diff --check passes. Repository-wide mypy reports four Windows-platform errors in unchanged Linux deployment files (grp.getgrnam, os.chown twice, os.mkfifo); Linux CI remains the full type-check gate.

The first pytest invocation inherited development settings and failed during test-database creation before any migration ran. Subsequent invocations explicitly select --ds=config.settings.test. No database roles or access controls were changed.

Outstanding gates: PostgreSQL 17/PostGIS execution, normalized pg_dump comparison, GitHub CI full suite and deployment-security checks. No Git push, workflow dispatch or deployment was performed. No blocking semantic schema differences have been identified on PostgreSQL 16; PostgreSQL 17 equivalence is unverified.
