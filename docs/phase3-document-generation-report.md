# Phase 3 local implementation report

The Fake-iPad client now accepts only generic Document Generation v2 for Fire
Plans, KLGV Plans, Department Files and Station Files. No backend schema or
installation-state changes were necessary.

Modified files:

- `tools/fake_ipad/generations.py`: generic endpoint, mandatory matching
  `dataset_type`, generic format/domain, explicit integer version/schema checks,
  exact Fire Plan metadata without `path`.
- `tools/fake_ipad/client.py`: generic descriptor URL for every document dataset;
  reject non-schema-2/non-document delivery for all four known document datasets,
  including optional obsolete Fire Plan ZIP descriptors.
- `tools/fake_ipad/datasets.py`: remove ZIP parser, dispatch, extension and ZIP
  imports; Fire Plan capability is exclusively document-manifest-v2/schema 2.
- `tools/fake_ipad/test_generations.py`: canonical synthetic server fixtures,
  explicit legacy rejection, Fire Plan cryptographic failures, exact authenticated
  HPKE info bytes, strict metadata/numeric checks, empty collections, unconditional
  304 rejection, and hydrant zero/invalid diameter checks.
- `tools/test_fake_ipad.py`: remove ZIP-only fixtures and obsolete positive ZIP tests.
- `docs/FireDash App — Incremental Document Dataset Provisioning Guide.md`:
  record canonical Fake-iPad support and link the acceptance checklist.

Created this report and `docs/phase3-document-acceptance.md`. No files were deleted.
No migrations were created or edited. Removed code includes `validate_fire_plans`,
the old Fire Plan endpoint/format/HPKE selection and schema-1 ZIP acceptance.

All documents use `document-generation-v2`,
`firedash-document-generation-hpke-v2`, and `/document-generations/`.
Existing Ed25519, P-256 HPKE, AES-KW and AES-GCM verification remains in place.
No changes were made to key persistence, CLI command definitions, JSON/GeoJSON
validators, main-manifest ETags, progress/output code, snapshot accounting or
ordinary dataset-key-grant handling. PDF plaintext remains in memory.

## Final targeted validation

Commands were run in PowerShell from the repository root with
`$env:DJANGO_SETTINGS_MODULE='config.settings.test'`. Only targeted client tests
and the small backend selection below were executed; not the complete suite.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tools/fake_ipad/test_generations.py tools/fake_ipad/test_output.py tools/fake_ipad/test_cli.py tools/test_fake_ipad.py -q --tb=short
```

**300 passed, 1 skipped in 6.48s.** The existing owner-only POSIX file-mode
test is skipped on Windows. These tests use disposable synthetic installation
state and simulated HTTP. They cover nine datasets, empty generations, metadata,
signature/HPKE/AES-KW/GCM/hash failures, installation and scope binding,
required/optional datasets, complete snapshot persistence, output modes,
download counts, progress and conditional/pending behavior.

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --reuse-db apps/publications/tests/test_fire_plan_v2_delivery.py::test_v2_manifest_grant_and_document_delivery apps/publications/tests/test_fire_plan_v2_delivery.py::test_authorization_precedes_matching_artifact_etag apps/publications/tests/test_fire_plan_v2_delivery.py::test_nonready_grant_cannot_download apps/publications/tests/test_fire_plan_v2_delivery.py::test_ready_grant_for_another_installation_does_not_authorize_download apps/publications/tests/test_fire_plan_v2_delivery.py::test_generic_fire_plan_worker_failure_preserves_current_publication apps/publications/tests/test_fire_plan_v2_delivery.py::test_obsolete_routes_and_schema_entries_are_absent apps/publications/tests/test_document_migrations.py::test_fresh_chain_and_cleanup_queue_rename_preserve_data -q --tb=short
```

**15 passed in 49.22s.** Uses the configured local PostgreSQL test database;
the migration fixture creates/drops its own disposable PostgreSQL database.
This confirms real signed generic delivery, pending/ready grants, decryption,
authorization before ETag, rejected nonready/wrong-installation grants,
failed-build preservation, absent old routes, and fresh migration/cleanup-row
preservation invariants. No live server database was accessed.

```powershell
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.venv\Scripts\ruff.exe check tools/fake_ipad/client.py tools/fake_ipad/datasets.py tools/fake_ipad/generations.py tools/fake_ipad/test_generations.py tools/test_fake_ipad.py
.venv\Scripts\ruff.exe format --check tools/fake_ipad/client.py tools/fake_ipad/datasets.py tools/fake_ipad/generations.py tools/fake_ipad/test_generations.py tools/test_fake_ipad.py
git -c safe.directory=C:/Users/derme/Documents/FireDash-Server diff --check
```

Results: zero Django issues; no migration changes; Ruff passed; five files
already formatted; diff check passed. Per-command `safe.directory` did not
write Git configuration.

Searches for the three obsolete endpoint/format/domain identifiers found no
matches in active Fake-iPad runtime code or Markdown documentation. Intentional
negative test fixtures retain them. No current test failures remain.

During development an encoding error interrupted the initial edit script, and
the ZIP removal initially included two adjacent shared Dangerous Goods constants.
Initial client runs failed (74 failures, then 64 failures); restoring the shared
constants also required restoring the import removed by Ruff, caught as four
collection errors. All were corrected before the passing intermediate runs
(252, 137, 296 and four selected tests) and final 300-test run above. Counts
across runs overlap and must not be summed as unique coverage.

## Remaining integration and operational work

Local acceptance is complete. Live acceptance has deliberately not run.
Phase 2 migrations 0032–0033, old server publication disposition, fresh generic
Fire Plan publication and rollout order still require owner review and separate
authorization. Follow [the owner-controlled acceptance checklist](phase3-document-acceptance.md)
and the [historical data/ciphertext procedure](document-generation-v2-operations.md).
Historical counts are expectations only, never client validation constants.

No Git writes, deployment, live migration, server-record changes, credential
regeneration, adoption/reset, real-tablet actions or requests to the live
FireDash server were performed. The existing `.firedash-fake-ipad` directory
was neither read nor changed; all test identities and histories were isolated.
