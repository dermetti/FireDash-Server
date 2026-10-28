# Phase 2 implementation report

Implemented locally on 2026-10-28. No outstanding implementation tasks or known
failing targeted tests remain. The complete repository suite was not run.

## Canonical architecture

All four document datasets use the generic adapter -> accepted-PDF SHA-256 check
-> identity/content-scoped DocumentArtifact creation or verified reuse -> exact
PublicationDocumentArtifactReference membership -> DocumentGenerationKey -> signed
DocumentGenerationManifest -> normal publication finalization path. Generation
manifest descriptors and ciphertext URLs use `/api/v1/tablet/document-generations/`.
The shared worker creates installation-bound DocumentGenerationKeyGrant records.

The Fire Plan ZIP builder, separate generation modules, models, routes, HPKE
context/domain, cutover action and delivery-format selector are removed. There
are no redirects, fallback descriptors or compatibility parsers. Newly frozen
Fire Plan snapshots contain all operational metadata and no ZIP member path.
Generic HPKE is an independent dataclass with unchanged wire/info bytes and the
`firedash-document-generation-hpke-v2` domain. DatasetKeyGrant remains intact.

Artifact reuse streams ciphertext in 1 MiB chunks, checking canonical path,
availability, exact size and SHA-256. Corruption fails the new generation without
replacing or deleting a shared artifact. Manifest readiness rejects malformed
wrapped-CEK types and mismatched signed metadata with expected validation errors.

Cleanup uses only generic artifacts/references and the renamed durable queue.
Terminal release locks artifacts and rechecks references; physical deletion runs
after commit with retryable queue records and generated-path/live-record checks.
Failed builds track only their newly allocated IDs for rollback compensation.
Unclassified historical ciphertext is never removed by a filesystem age scan.
Normal restoration of complete generic generations remains supported, including
restoring an older PDF after source replacement.

## Migrations

- **0032**: explicitly removes the two legacy artifact/reference triggers and
  their functions; drops FirePlanDocumentArtifact, PublicationFirePlanArtifactReference,
  FirePlanGenerationKey, FirePlanGenerationKeyGrant and FirePlanGenerationManifest
  in dependency order;
  renames FirePlanDocumentArtifactCleanup to DocumentArtifactCleanup, preserving
  rows, timestamps and unique artifact ID/path fields; removes delivery_format.
  Table and column drops are restrictive, without CASCADE. Django state uses
  DeleteModel, RenameModel and RemoveField operations. This is forward-only DDL.
- **0033**: updates the existing generic readiness function to compare complete
  current metadata without stripping a Fire Plan ZIP path, and requires JSON
  string wrapped CEKs and numeric size/version fields. The generic lifecycle,
  reference, cryptographic-delete and File Library immutability guards remain.
- Historical migrations, including 0017-0020 and 0031, were not edited.
- Disposable PostgreSQL tests prove the fresh chain, upgrade through 0031,
  data-preserving queue rename, removal of obsolete tables/triggers/functions,
  continued generic guards, and rejection of incomplete/malformed generations.

## Files

Created:

- `apps/publications/migrations/0032_remove_legacy_fire_plan_delivery.py`
- `apps/publications/migrations/0033_strict_document_generation_metadata.py`
- `apps/publications/tests/conftest.py`
- `apps/publications/tests/test_document_migrations.py`
- `docs/document-generation-v2-operations.md`
- `docs/phase2-document-generation-report.md`

Modified:

- `apps/publications/artifacts.py`
- `apps/publications/builders.py`
- `apps/publications/document_artifacts.py`
- `apps/publications/document_authorization.py`
- `apps/publications/document_dataset_adapters.py`
- `apps/publications/document_v2.py`
- `apps/publications/hpke.py`
- `apps/publications/manifests.py`
- `apps/publications/models.py`
- `apps/publications/paths.py`
- `apps/publications/services.py`
- `apps/publications/tests/test_artifact_ceiling.py`
- `apps/publications/tests/test_artifacts.py`
- `apps/publications/tests/test_bulk_intent_migration.py`
- `apps/publications/tests/test_fire_plan_v2.py`
- `apps/publications/tests/test_fire_plan_v2_delivery.py`
- `apps/publications/tests/test_hpke.py`
- `apps/publications/tests/test_scope_migration_upgrade.py`
- `apps/publications/tests/test_source_fingerprints.py`
- `apps/publications/urls.py`
- `apps/publications/views.py`
- `apps/tablets/api.py`
- `apps/tablets/api_urls.py`
- `apps/tablets/tests/test_api.py`
- `docs/FireDash App — Incremental Document Dataset Provisioning Guide.md`
- `docs/architecture.md`
- `docs/configuration.md`
- `docs/tablet-api.md`
- `templates/publications/scope_detail.html`

Removed:

- `apps/publications/fire_plan_v2.py`
- `apps/publications/fire_plan_v2_delivery.py`
- `apps/publications/tests/test_document_artifacts.py`
- `apps/publications/tests/test_fire_plan_v2_retention.py`

## Targeted validation

All commands below used PowerShell with
`$env:DJANGO_SETTINGS_MODULE='config.settings.test'` and the configured local
TEST_POSTGRES connection. Migration tests used independently generated disposable
PostgreSQL databases, with restrictive removal of only those generated databases.
No live test-server or production migration was run. No full-suite command ran.

The one platform skip requires native Unix group ownership on this Windows host.
Intermediate failures listed below were resolved; no final failure is outstanding.
A collection-only attempt contained a mistyped test node and ran zero tests; its
corrected command is included below. One earlier selection was interrupted before
test execution and narrowed to the relevant tablet nodes.

### Initial focused regression

74 passed, 1 failed in 158.15s; stale removed-relation assertion corrected.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/publications/tests/test_fire_plan_manifest.py apps/publications/tests/test_klgv_manifest.py apps/publications/tests/test_worker_lanes.py apps/publications/tests/test_publication_lifecycle.py -q --tb=short
```

### Initial hardening

74 passed, 2 failed in 181.62s; temporary-database alias and obsolete-route test setup corrected.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/publications/tests/test_hpke.py apps/publications/tests/test_document_migrations.py -q --tb=short
```

### Targeted integration

164 passed, 4 failed, 1 skipped in 289.81s. Historical migration fixtures and isolated filesystem-test mocking corrected below.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_document_migrations.py apps/publications/tests/test_bulk_intent_migration.py apps/publications/tests/test_scope_migration_upgrade.py apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/publications/tests/test_hpke.py apps/file_library/tests/test_publication.py apps/file_library/tests/test_publication_reverts.py apps/publications/tests/test_retention.py apps/publications/tests/test_artifacts.py -q --tb=short
```

### Corrected integration rerun

79 passed, 1 failed in 242.14s. The remaining historical scope-migration fixture was corrected and passed separately below.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_document_migrations.py apps/publications/tests/test_bulk_intent_migration.py apps/publications/tests/test_scope_migration_upgrade.py apps/publications/tests/test_fire_plan_v2_delivery.py apps/publications/tests/test_fire_plan_v2.py apps/file_library/tests/test_publication.py::test_frozen_metadata_reuse_replacement_and_retention apps/file_library/tests/test_publication.py::test_both_file_scopes_deliver_signed_individual_pdfs_and_authoritative_empty apps/publications/tests/test_artifacts.py::test_cleanup_removes_stale_temp_directories -q --tb=short
```

### Corrected historical scope upgrade

1 passed in 24.63s.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_scope_migration_upgrade.py -q --tb=short
```

### Final contract and current reuse code

64 passed in 96.77s.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_document_migrations.py apps/publications/tests/test_fire_plan_v2.py apps/publications/tests/test_klgv_manifest.py apps/publications/tests/test_fire_plan_manifest.py apps/publications/tests/test_worker_lanes.py apps/publications/tests/test_artifact_ceiling.py apps/publications/tests/test_source_fingerprints.py::test_document_source_manifest_uses_stable_sanitized_pdf_hash_and_logical_metadata apps/publications/tests/test_source_fingerprints.py::test_source_diff_uses_the_canonical_payload_and_hides_pdf_hash_values apps/publications/tests/test_grants.py::test_grant_becomes_ready_after_worker_processing apps/publications/tests/test_grants.py::test_failed_required_grant_fails_manifest_without_reclaiming_it apps/publications/tests/test_manifests.py::test_web_manifest_queues_grant_without_reading_kek_and_worker_fulfills_it apps/tablets/tests/test_api.py::test_current_document_generation_is_discovered_without_a_v1_grant apps/tablets/tests/test_api.py::test_manifest_pending_is_an_rfc9457_problem apps/tablets/tests/test_api.py::test_download_uses_canonical_artifact_path_and_etag apps/tablets/tests/test_api.py::test_download_errors_stay_problem_json_with_octet_stream_accept apps/tablets/tests/test_api.py::test_document_generation_openapi_matches_json_binary_and_pending_contracts apps/tablets/tests/test_api.py::test_openapi_tablet_error_and_conditional_response_contracts -q --tb=short
```

### Final restrictive-DDL fresh migration chain

1 passed in 18.65s.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_document_migrations.py -q --tb=short
```

## Static checks and remaining operations

Passed: Django system check (0 issues); migration consistency (no changes);
Ruff check; Ruff formatting check (109 files already formatted); git diff --check.
Runtime-code and canonical API-documentation searches found no obsolete
protocol, model, delivery selector or cutover references. Historical migrations,
negative tests and this explanatory report retain historical identifiers.

```powershell
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.venv\Scripts\ruff.exe check apps/publications apps/tablets/api.py apps/tablets/api_urls.py apps/tablets/tests/test_api.py
.venv\Scripts\ruff.exe format --check apps/publications apps/tablets/api.py apps/tablets/api_urls.py apps/tablets/tests/test_api.py
git -c safe.directory=C:/Users/derme/Documents/FireDash-Server diff --check
```

No Git writes, deployment, live server-data changes, installation/adoption resets,
tablet actions or Fake-iPad code changes were performed. No private credentials
were printed. Phase 3 client consolidation and live acceptance remain outside
this implementation.

Historical ciphertext and disposable old publications need separately authorized
operational handling; see [the rollout and cleanup procedure](document-generation-v2-operations.md).
Inventory old paths before dropping their tables, preserve all live generic
artifacts and durable queue records, and rebuild/dispose old snapshots explicitly.
A process killed between physical promotion and compensation can leave an
untracked file; its classification requires an authorized audit, not age-based
maintenance deletion. Neither migration performs filesystem cleanup.
