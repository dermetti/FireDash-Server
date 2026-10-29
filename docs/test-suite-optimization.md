# Server test suite — Phase 3.5

## Policy and scope

Python tests cover persisted state, authorization, validation, HTTP/API protocols,
database behavior and integration. Automated appearance verification has been
discontinued: buttons, copy, CSS, icons, navigation, DOM structure, widget styling
and browser interaction are manual acceptance concerns. Tests using Django Client
remain server tests. Escaping, secret non-disclosure, tenant boundaries, CSRF,
cache/privacy headers and meaningful protocol responses remain automated.

All collected modules under `apps/`, `config/` and `tools/` were inventoried by
test function/assertion, then output/rendering candidates and their helpers were
inspected. Filename suffixes did not determine deletion. CLI diagnostic/error
contracts, JSON output and redaction tests remain functional protocol coverage;
they are not browser appearance tests. No production Python, schema, migration,
cryptography, retention or authorization contract was changed.

The historical 2,039 passed / 22 failed / 14 skipped, 1,131.13-second full run is
not green. The older 1,262-test measurement is not a comparable performance
baseline. No full execution was performed in this phase.

## Functional changes to tests

- Mixed tests now use view context, bound forms and persisted data for list
  membership, pagination, filtering, ordering, historical authority, error handling
  and publication state. Server mutations, access matrices and their negative
  cases remain. Report overview assertions use report IDs, ordered header data
  and photo counts; HTML escaping and all filename/download checks remain.
- Browser harnesses were deleted. The move mutation formerly exercised through
  a browser is covered directly for both normal roles by explorer operation and
  destination-pagination tests. No browser automation replacement was added.
- The existing disposable publication migration fixture moved to root
  `conftest.py` and accepts an app/migration target. Historical tests start in an
  independent empty PostGIS-template database and upgrade forward. They never
  downgrade a database through irreversible publication migration 0033.
  Quota tests compare historical job columns across the upgrade and still verify
  audit, document, content, policy and revision preservation. The quota data
  migration's no-op reverse is tested directly, without reversing unrelated DDL.
- Fourteen wizard cases use rollback-based `django_db`. They test one-connection
  preview/review/apply state, not commit visibility. Real commits, worker locks,
  concurrent connections, migration tests and callback guarantees retain
  `transaction=True`. In particular, the rebuilt publication commit test uses a
  separate worker connection to prove staged work is invisible before commit.
- Session-scoped RSA fixtures already avoid repeated expensive report key
  generation; they were left intact. No shared mutable database fixtures or
  mocked core behavior were introduced. Test registry/audit restoration, database
  roles, PostGIS and serial scratch cleanup remain unchanged.

## The 22 previously reported failures

| Family | Previous failures | Cause and correction |
|---|---:|---|
| Ingestion preview | 2 | Migration `ingestion.0005` intentionally seeds the SYSTEM goods scope. Compare complete before/after scope rows instead of assuming a globally empty database; retain canonical, audit and apply assertions. The same assumption also surfaced in cancellation during targeted reproduction and was corrected. |
| System administration | 2 | Current Data Hub includes SYSTEM goods as well as rescue configuration. Update structured context expectations; discard icons/copy/card checks. Keep role denial, policy mutation, audit and side-effect checks. |
| History, rebuild and retention | 5 | Reuse the migration-seeded SYSTEM goods scope with `get_or_create`; retain unique constraints and mixed-owner/predecessor/retention assertions. No blanket queue or scope deletion. |
| Read-path queries | 3 | Two batched live-attempt guards in `_decorate_scope_rows` legitimately add constant reads. Keep an eight-query ceiling at 4, 50 and 400 scopes, page enrichment limits, no source rebuild and no JSON payload reads. The empty SYSTEM panel remains empty until a current publication exists. |
| File Library / hydrant migrations | 4 | Unsupported downgrades through irreversible publication 0033. Test supported forward upgrades in disposable databases, including installation of library guards and registry entries. |
| Rebuild/debounce/wake | 4 | Staged work is allocated synchronously in the caller transaction; duplicate explicit rebuilds are rejected intentionally by the existing service and independent endpoint tests. Verify rejection leaves work unchanged and schedules no second wake. Verify worker visibility after commit. Exercise low-level pending coalescing through `enqueue_publication_job`, not the deliberately rejecting public rebuild service. |
| Fire-plan lifecycle | 1 | The legacy JSON artifact builder was retired in favor of document generations. Assert inactive plans are absent from the supported source payload; preserve edit, audit, lifecycle, deletion and scope checks. Existing document-generation delivery tests retain actual encrypted artifact coverage. |
| Tablet migration | 1 | Mixed forward/backward target graph. Begin at historical tablet 0005 in the disposable database, upgrade forward, and verify only REMOVED tablets and owned dependents disappear. |

No model constraint, irreversible safeguard, authorization check or production
rebuild behavior was weakened. Intermediate failures while converting assertions
are listed with validation below; a later full release run is still required.

## Development workflow

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'

# Focused: choose changed modules plus their dependencies.
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/ingestion/tests/test_review_wizard.py --durations=10

# Routine backend regression: automatic collection, only extended workloads excluded.
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider -m 'not slow' -ra --durations=30

# Complete release acceptance: every retained functional test, including slow workloads.
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider -ra --durations=30

# Collection, without test execution.
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider --collect-only -q

.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
```

The two existing `slow` cases are accepted 50,000-feature ingestion and the
50,000-hydrant builder; rejected-input limits remain routine. Security,
concurrency, migrations, API contracts, crypto, publication lifecycle, reports,
mail and independent Fake-iPad acceptance are not excluded for being expensive.
No hand-maintained routine allowlist and no xdist were introduced. CI keeps its
complete server-suite gate. Run pytest serially; separate workers' database and
scratch isolation have not been validated.

The repository-wide routine command was not executed during this bounded task:
it differs from complete collection by only two cases and cannot reasonably be
budgeted as a short additional run after the previous 19-minute baseline.
Validation instead uses affected modules and explicit security/integration
regressions. This is not a claim of green routine or full regression.

## Platform and release risks

The six old browser platform skips disappear with their obsolete harnesses.
Retained platform-specific tests require Unix artifact group ownership (one),
Linux sanitizer broker `dir_fd`/`O_NOFOLLOW`/ownership/FIFO behavior (six), and
POSIX Fake-iPad owner-only modes (one). They remain release checks on Linux;
Windows cannot certify those guarantees. No new skip, xfail or failing-test
exclusion was introduced.

A later complete release run must check interaction/order effects across all
retained modules and extended workloads, on Linux as well as this Windows
environment. Disposable forward-only migration tests are intentionally more
expensive than a failed downgrade; correctness takes precedence. No aggregate
full-suite speed improvement is claimed without a comparable completed run.

Critical retained coverage includes `accounts/tests` (login, MFA, throttling,
reauthentication and CSRF), `authorization/tests` and scoped management endpoints,
`tablets/tests/test_api.py`, personnel/recipient/domain policy tests, publication
constraints and forward migrations, lock/concurrency and worker tests, dataset
signing/HPKE/document delivery, ReportDocument/JWE and upload idempotency,
immutable transfer snapshots, token/session revocation, download/audit/retention,
outbound-mail admission/provider/outcome tests, and independent Fake-iPad wire
acceptance. Their collection is automatic in both routine and complete tiers;
the two extended workload exclusions do not remove any of these categories.

## Inventory and validation

The tables below record this working tree's collection and bounded validation.

An initial profiling invocation inherited the wrong Django settings and failed
at test-database creation before running tests. It was discarded; every measured
run below explicitly selects `config.settings.test`. No database privileges or
connection policy were changed to work around that refusal.

### Consolidated functional scenarios

| Removed duplicate | Surviving coverage |
|---|---|
| `accounts/test_auth_shell.py::test_invalid_login_returns_single_complete_page_with_one_error` | `test_authentication_integration.py::test_login_and_mfa_failures_return_generic_errors`: rejected credentials, generic failure, no submitted password leakage. Single-page HTML/error counts were presentation and discontinued. |
| `file_library/test_explorer_ui.py::test_move_destination_navigation_keeps_bootstrap_modal_mounted_in_browser` | Same module's `test_same_parent_explorer_navigation_and_full_operations` and `test_move_destination_pagination_and_mutation`, both department/station cases: authorized move, destination, preserved identity and persisted parent. Modal mounting is discontinued. |
| `tablets/test_management_ui.py::test_create_tablet_hx_modal_load` | `test_create_tablet_requires_display_name` also checks the authorized HTMX GET, then invalid POST and unchanged database. |
| `tablets/test_management_ui.py::test_assign_vehicle_audits_event` | `test_assign_vehicle_same_department`: same real POST, persisted assignment and audit tied to that assignment UUID. |
| `tablets/test_views.py::test_department_admin_can_view_tablet_list_and_detail` | `test_management_ui.py::test_department_admin_can_view_tablet_pages` covers both endpoints plus status summary. Installation telemetry has dedicated API/service tests. |
| `tablets/test_views.py::test_station_admin_is_denied_tablet_pages` | Identically named `test_management_ui.py` negative case, same list/detail endpoints and 403 outcomes. |
| `tablets/test_views.py::test_department_admin_is_denied_cross_department_tablets` | `test_management_ui.py::test_other_department_admin_is_denied`, same persisted foreign tablet/list/detail 403 boundary. |

The real-HTMX publication browser test's recorded search/filter requests are
covered by direct `test_scope_then_status_filters`,
`test_station_search_bounded_fallback_and_authority`, pagination/reset and invalid
station tests in `test_scope_filters.py`. Browser request orchestration and DOM
behavior themselves are discontinued. Its browser-only assertions, and other
static UI assertions, have no replacement visual checks under another name.

### Collection inventory
Before: **2,075 cases in 170 collected modules**. After: **1,998 cases in 167 modules**. **77 cases physically removed: 70 presentation cases (57 functions) and seven redundant functional cases**. Five function renames preserve or strengthen backend scenarios, including the forward-only library upgrade, direct destination pagination and pending MFA enrollment. No new skip/xfail or collection exclusion was added.
| Module | Before cases | After cases |
|---|---:|---:|
| `apps/accounts/tests/test_auth_page_security_headers.py` | 3 | 3 |
| `apps/accounts/tests/test_auth_shell.py` | 6 | 0 |
| `apps/accounts/tests/test_authentication_integration.py` | 10 | 10 |
| `apps/accounts/tests/test_bootstrap_system_admin.py` | 5 | 5 |
| `apps/accounts/tests/test_mfa_enrollment.py` | 1 | 1 |
| `apps/accounts/tests/test_models.py` | 2 | 2 |
| `apps/accounts/tests/test_reissue_system_admin_setup.py` | 13 | 13 |
| `apps/application_secrets/tests/test_crypto.py` | 15 | 15 |
| `apps/audit/tests/test_middleware.py` | 2 | 2 |
| `apps/authorization/tests/test_administrator_lifecycle.py` | 5 | 5 |
| `apps/authorization/tests/test_managed_mail_eligibility.py` | 4 | 4 |
| `apps/authorization/tests/test_system_admin_state.py` | 5 | 5 |
| `apps/authorization/tests/test_vehicle_rescue_guides.py` | 3 | 3 |
| `apps/file_library/tests/test_backend_preparation.py` | 27 | 27 |
| `apps/file_library/tests/test_explorer_ui.py` | 23 | 20 |
| `apps/file_library/tests/test_libraries.py` | 28 | 28 |
| `apps/file_library/tests/test_management_ux.py` | 17 | 15 |
| `apps/file_library/tests/test_publication.py` | 17 | 17 |
| `apps/file_library/tests/test_publication_reverts.py` | 43 | 43 |
| `apps/file_library/tests/test_review_presentation.py` | 3 | 3 |
| `apps/file_library/tests/test_stage4_quotas.py` | 5 | 5 |
| `apps/file_library/tests/test_temporary_access.py` | 11 | 11 |
| `apps/health/tests/test_health.py` | 3 | 3 |
| `apps/ingestion/tests/test_dangerous_goods_integration.py` | 6 | 6 |
| `apps/ingestion/tests/test_dangerous_goods_migration.py` | 1 | 1 |
| `apps/ingestion/tests/test_dangerous_goods_tablet_contract.py` | 2 | 2 |
| `apps/ingestion/tests/test_dangerous_goods_ui.py` | 8 | 6 |
| `apps/ingestion/tests/test_dangerous_goods_validation.py` | 5 | 5 |
| `apps/ingestion/tests/test_fire_plan_operational_metadata.py` | 2 | 2 |
| `apps/ingestion/tests/test_hydrant_convergence.py` | 10 | 10 |
| `apps/ingestion/tests/test_hydrant_location_contract.py` | 5 | 5 |
| `apps/ingestion/tests/test_hydrant_preview_scaling.py` | 9 | 9 |
| `apps/ingestion/tests/test_limits.py` | 9 | 9 |
| `apps/ingestion/tests/test_maintenance.py` | 1 | 1 |
| `apps/ingestion/tests/test_partial_documents.py` | 9 | 9 |
| `apps/ingestion/tests/test_pdf_packages.py` | 17 | 17 |
| `apps/ingestion/tests/test_personnel_home_station_import.py` | 7 | 6 |
| `apps/ingestion/tests/test_phonebook_import.py` | 5 | 5 |
| `apps/ingestion/tests/test_review_wizard.py` | 14 | 14 |
| `apps/ingestion/tests/test_sanitizer_logging.py` | 3 | 3 |
| `apps/ingestion/tests/test_services.py` | 30 | 30 |
| `apps/ingestion/tests/test_station_vehicle_import.py` | 11 | 11 |
| `apps/ingestion/tests/test_templates.py` | 5 | 5 |
| `apps/ingestion/tests/test_unicode_roundtrip.py` | 1 | 1 |
| `apps/ingestion/tests/test_update_review.py` | 6 | 6 |
| `apps/ingestion/tests/test_views.py` | 16 | 6 |
| `apps/organizations/tests/test_department_presentation.py` | 5 | 5 |
| `apps/organizations/tests/test_stations.py` | 3 | 3 |
| `apps/organizations/tests/test_vehicle_retirement.py` | 3 | 3 |
| `apps/outbound_mail/tests/test_brevo.py` | 18 | 18 |
| `apps/outbound_mail/tests/test_department_services.py` | 9 | 9 |
| `apps/outbound_mail/tests/test_network_policy.py` | 5 | 5 |
| `apps/outbound_mail/tests/test_report_admission.py` | 6 | 6 |
| `apps/outbound_mail/tests/test_report_delivery.py` | 8 | 8 |
| `apps/outbound_mail/tests/test_report_delivery_requests.py` | 7 | 7 |
| `apps/outbound_mail/tests/test_resolution.py` | 6 | 6 |
| `apps/outbound_mail/tests/test_runtime.py` | 13 | 13 |
| `apps/outbound_mail/tests/test_secret_rotation.py` | 3 | 3 |
| `apps/outbound_mail/tests/test_services.py` | 11 | 11 |
| `apps/outbound_mail/tests/test_smtp.py` | 11 | 11 |
| `apps/personnel/tests/test_lifecycle.py` | 3 | 3 |
| `apps/personnel/tests/test_management_ui.py` | 3 | 3 |
| `apps/personnel/tests/test_phase2c_ui.py` | 2 | 1 |
| `apps/personnel/tests/test_views.py` | 9 | 9 |
| `apps/portal/tests/test_administrator_list.py` | 10 | 10 |
| `apps/portal/tests/test_administrator_provisioning.py` | 7 | 7 |
| `apps/portal/tests/test_backlog_phase1_ui.py` | 2 | 2 |
| `apps/portal/tests/test_data_hub_publication_state.py` | 16 | 12 |
| `apps/portal/tests/test_department_outbound_email.py` | 2 | 2 |
| `apps/portal/tests/test_navigation.py` | 11 | 6 |
| `apps/portal/tests/test_overview.py` | 6 | 4 |
| `apps/portal/tests/test_permissions.py` | 7 | 7 |
| `apps/portal/tests/test_quota_settings.py` | 16 | 16 |
| `apps/portal/tests/test_stage4_department_administration.py` | 15 | 12 |
| `apps/portal/tests/test_stage5_system_administration.py` | 10 | 8 |
| `apps/portal/tests/test_station_admin_boundaries.py` | 7 | 7 |
| `apps/portal/tests/test_station_vehicle_management.py` | 8 | 8 |
| `apps/portal/tests/test_system_outbound_email.py` | 9 | 7 |
| `apps/publications/tests/test_artifact_ceiling.py` | 4 | 4 |
| `apps/publications/tests/test_artifact_layout.py` | 5 | 5 |
| `apps/publications/tests/test_artifacts.py` | 18 | 18 |
| `apps/publications/tests/test_attempt_versioning.py` | 5 | 5 |
| `apps/publications/tests/test_bulk_intent_migration.py` | 2 | 2 |
| `apps/publications/tests/test_bulk_rebuild.py` | 4 | 4 |
| `apps/publications/tests/test_debounce.py` | 13 | 13 |
| `apps/publications/tests/test_deployment_lanes.py` | 8 | 8 |
| `apps/publications/tests/test_document_migrations.py` | 1 | 1 |
| `apps/publications/tests/test_fire_plan_manifest.py` | 5 | 5 |
| `apps/publications/tests/test_fire_plan_v2.py` | 28 | 28 |
| `apps/publications/tests/test_fire_plan_v2_delivery.py` | 79 | 79 |
| `apps/publications/tests/test_grants.py` | 15 | 15 |
| `apps/publications/tests/test_history_queries.py` | 17 | 17 |
| `apps/publications/tests/test_hpke.py` | 8 | 8 |
| `apps/publications/tests/test_klgv_manifest.py` | 6 | 6 |
| `apps/publications/tests/test_manifest_contract.py` | 2 | 2 |
| `apps/publications/tests/test_manifests.py` | 5 | 5 |
| `apps/publications/tests/test_pdf_bundles.py` | 6 | 6 |
| `apps/publications/tests/test_phase4a_lifecycle.py` | 14 | 14 |
| `apps/publications/tests/test_phase4b_locking.py` | 3 | 3 |
| `apps/publications/tests/test_phase6.py` | 14 | 14 |
| `apps/publications/tests/test_phonebook_publications.py` | 4 | 4 |
| `apps/publications/tests/test_publication_lifecycle.py` | 8 | 8 |
| `apps/publications/tests/test_publications_ui.py` | 31 | 29 |
| `apps/publications/tests/test_read_path_performance.py` | 4 | 4 |
| `apps/publications/tests/test_retention.py` | 8 | 7 |
| `apps/publications/tests/test_retention_hardening.py` | 28 | 28 |
| `apps/publications/tests/test_scope_filters.py` | 34 | 31 |
| `apps/publications/tests/test_scope_migration_upgrade.py` | 1 | 1 |
| `apps/publications/tests/test_signing_keys.py` | 8 | 8 |
| `apps/publications/tests/test_source_fingerprints.py` | 4 | 4 |
| `apps/publications/tests/test_state.py` | 3 | 3 |
| `apps/publications/tests/test_summary_validation.py` | 19 | 19 |
| `apps/publications/tests/test_system_scope.py` | 3 | 3 |
| `apps/publications/tests/test_wake.py` | 11 | 11 |
| `apps/publications/tests/test_worker_failure.py` | 3 | 3 |
| `apps/publications/tests/test_worker_lanes.py` | 9 | 9 |
| `apps/reference_data/tests/test_document_lifecycle.py` | 5 | 5 |
| `apps/reference_data/tests/test_document_management_ui.py` | 3 | 2 |
| `apps/reference_data/tests/test_fire_plan_form.py` | 4 | 4 |
| `apps/reference_data/tests/test_fire_plan_identity.py` | 1 | 1 |
| `apps/reference_data/tests/test_fire_plan_operational_metadata.py` | 2 | 2 |
| `apps/reference_data/tests/test_fire_plan_view.py` | 1 | 1 |
| `apps/reference_data/tests/test_hydrant_list.py` | 8 | 8 |
| `apps/reference_data/tests/test_hydrant_location_migration.py` | 1 | 1 |
| `apps/reference_data/tests/test_hydrant_management_ui.py` | 4 | 4 |
| `apps/reference_data/tests/test_hydrants.py` | 15 | 15 |
| `apps/reference_data/tests/test_pdf_sandbox.py` | 10 | 10 |
| `apps/reference_data/tests/test_pdf_sanitizer_broker.py` | 36 | 36 |
| `apps/reference_data/tests/test_pdf_validation.py` | 7 | 7 |
| `apps/reference_data/tests/test_phonebook.py` | 12 | 11 |
| `apps/reference_data/tests/test_storage.py` | 9 | 9 |
| `apps/report_transfer/tests/test_authorization_audit.py` | 36 | 36 |
| `apps/report_transfer/tests/test_documents.py` | 59 | 59 |
| `apps/report_transfer/tests/test_download_filenames.py` | 7 | 7 |
| `apps/report_transfer/tests/test_portal.py` | 53 | 53 |
| `apps/report_transfer/tests/test_submissions.py` | 56 | 56 |
| `apps/tablets/tests/test_adoption_api_crypto.py` | 2 | 2 |
| `apps/tablets/tests/test_adoption_fixture.py` | 1 | 1 |
| `apps/tablets/tests/test_api.py` | 71 | 71 |
| `apps/tablets/tests/test_api_validation_logging.py` | 6 | 6 |
| `apps/tablets/tests/test_asset_number_generation.py` | 22 | 22 |
| `apps/tablets/tests/test_failure_counters.py` | 8 | 8 |
| `apps/tablets/tests/test_lifecycle.py` | 16 | 16 |
| `apps/tablets/tests/test_list_template_regressions.py` | 11 | 0 |
| `apps/tablets/tests/test_management_ui.py` | 28 | 22 |
| `apps/tablets/tests/test_provisioning_crypto.py` | 17 | 17 |
| `apps/tablets/tests/test_stage21_asset_lifecycle.py` | 12 | 12 |
| `apps/tablets/tests/test_stage21_detail_hierarchy.py` | 2 | 0 |
| `apps/tablets/tests/test_stage21_reprovision.py` | 1 | 1 |
| `apps/tablets/tests/test_stage21_stale.py` | 2 | 2 |
| `apps/tablets/tests/test_stage21_transfer.py` | 3 | 2 |
| `apps/tablets/tests/test_stage22_lifecycle.py` | 14 | 14 |
| `apps/tablets/tests/test_stage22_migration.py` | 1 | 1 |
| `apps/tablets/tests/test_stage2_api_activity.py` | 7 | 6 |
| `apps/tablets/tests/test_stage2_assignment.py` | 5 | 5 |
| `apps/tablets/tests/test_stage2_identity.py` | 7 | 7 |
| `apps/tablets/tests/test_stage2_list.py` | 12 | 10 |
| `apps/tablets/tests/test_stage2_origin.py` | 3 | 3 |
| `apps/tablets/tests/test_stage2_replacement.py` | 3 | 3 |
| `apps/tablets/tests/test_storage_migration.py` | 1 | 1 |
| `apps/tablets/tests/test_versions.py` | 17 | 17 |
| `apps/tablets/tests/test_views.py` | 4 | 1 |
| `config/tests/test_settings.py` | 8 | 8 |
| `config/tests/test_test_scratch_cleanup.py` | 2 | 2 |
| `tools/fake_ipad/test_cli.py` | 6 | 6 |
| `tools/fake_ipad/test_generations.py` | 156 | 156 |
| `tools/fake_ipad/test_output.py` | 34 | 34 |
| `tools/fake_ipad/test_reports.py` | 33 | 33 |
| `tools/test_fake_ipad.py` | 105 | 105 |
| `tools/test_fake_ipad_server_contract.py` | 2 | 2 |

### Exact deleted test inventory
Entire modules removed: `apps/accounts/tests/test_auth_shell.py`, `apps/tablets/tests/test_list_template_regressions.py`, and `apps/tablets/tests/test_stage21_detail_hierarchy.py`. The first module includes the consolidated login rejection case listed above; the two tablet modules are entirely presentation-only. Obsolete render fixtures, parsers, browser harnesses and unused setup/imports were removed with their checks.

`apps/accounts/tests/test_auth_shell.py`:

- `test_login_page_uses_vendored_bootstrap_without_cdn` ? presentation.
- `test_setup_page_uses_vendored_bootstrap_without_cdn` ? presentation.
- `test_login_form_has_no_htmx_submission_attributes` ? presentation.
- `test_invalid_login_returns_single_complete_page_with_one_error` ? functional consolidation.
- `test_overview_uses_authenticated_shell_not_auth_card` ? presentation.
- `test_reauthenticate_page_uses_bootstrap_form` ? presentation.

`apps/file_library/tests/test_explorer_ui.py`:

- `test_station_combobox_keyboard_pointer_and_stale_responses_in_browser` ? presentation.
- `test_move_destination_navigation_keeps_bootstrap_modal_mounted_in_browser` ? functional consolidation.

`apps/file_library/tests/test_management_ux.py`:

- `test_overview_has_no_file_navigation_tile_and_role_scoped_sidebar` ? presentation.

`apps/ingestion/tests/test_dangerous_goods_ui.py`:

- `test_upload_modal_get` ? presentation.
- `test_goods_modal_opens_and_handles_validation_in_browser` ? presentation.

`apps/ingestion/tests/test_personnel_home_station_import.py`:

- `test_personnel_import_page_template_uses_home_station_contract` ? presentation.

`apps/ingestion/tests/test_views.py`:

- `test_domain_import_pages_have_explicit_human_presentation` ? presentation.
- `test_shared_final_apply_form_announces_synchronous_processing` ? presentation.
- `test_applied_batch_no_longer_renders_apply_control` ? presentation.

`apps/personnel/tests/test_phase2c_ui.py`:

- `test_person_detail_explains_distinct_offboard_and_anonymize_consequences` ? presentation.

`apps/portal/tests/test_data_hub_publication_state.py`:

- `test_data_hub_cards_preserve_navigation_and_distinct_icons` ? presentation.
- `test_system_hub_preserves_rescue_configuration_without_email_card` ? presentation.

`apps/portal/tests/test_navigation.py`:

- `test_nav_has_no_blank_labels_or_urls` ? presentation.
- `test_authenticated_pages_render_shared_shell` ? presentation.
- `test_rendered_navigation_has_no_blank_links` ? presentation.
- `test_role_specific_navigation_matches_on_desktop_and_mobile` ? presentation.

`apps/portal/tests/test_overview.py`:

- `test_dashboard_renders_attention_first_quiet_state_and_accessible_operational_text` ? presentation.
- `test_system_overview_uses_orphan_recovery_only` ? presentation.

`apps/portal/tests/test_stage4_department_administration.py`:

- `test_department_navigation_overview_and_topbar_are_scoped` ? presentation.
- `test_attention_indicator_is_quiet_without_authoritative_attention` ? presentation.
- `test_department_search_lists_use_personnel_live_contract_and_results_fragments` ? presentation.

`apps/portal/tests/test_stage5_system_administration.py`:

- `test_system_navigation_is_isolated_and_all_roles_keep_their_scoped_entries` ? presentation.
- `test_system_search_lists_use_personnel_live_contract_and_return_only_results` ? presentation.

`apps/portal/tests/test_system_outbound_email.py`:

- `test_outbound_email_return_button_links_to_system_settings` ? presentation.
- `test_settings_card_adapts_with_htmx_and_cards_are_stacked` ? presentation.

`apps/publications/tests/test_publications_ui.py`:

- `test_update_is_separate_from_current_and_building_row_polls_only_itself` ? presentation.
- `test_current_row_does_not_offer_rollback_when_only_newer_superseded_version_exists` ? presentation.

`apps/publications/tests/test_retention.py`:

- `test_retention_purged_snapshot_is_not_offered_for_inspection` ? presentation.

`apps/publications/tests/test_scope_filters.py`:

- `test_scope_links_preserve_status_reset_page_and_clear_station` ? presentation.
- `test_publication_filters_with_real_htmx_in_browser` ? presentation.

`apps/reference_data/tests/test_document_management_ui.py`:

- `test_document_import_pages_are_domain_scoped` ? presentation.

`apps/reference_data/tests/test_phonebook.py`:

- `test_data_hub_search_inputs_render_the_personnel_live_search_contract` ? presentation.

`apps/tablets/tests/test_list_template_regressions.py`:

- `test_status_summary_polls_every_30s_without_load_trigger` ? presentation.
- `test_status_summary_has_single_polling_root` ? presentation.
- `test_status_summary_has_no_sub_second_polling` ? presentation.
- `test_results_table_participates_in_page_flow_without_scroll_wrapper` ? presentation.
- `test_results_table_keeps_identity_state_and_actions` ? presentation.
- `test_results_table_hides_secondary_columns_responsively` ? presentation.
- `test_list_uses_live_server_side_filters_with_a_one_second_search_debounce` ? presentation.
- `test_list_keeps_asset_and_installation_lifecycle_filters_distinct` ? presentation.
- `test_adoption_status_polls_every_4s_without_load_trigger` ? presentation.
- `test_adoption_status_waiting_state_is_self_replacing` ? presentation.
- `test_adoption_status_terminal_states_stop_polling` ? presentation.

`apps/tablets/tests/test_management_ui.py`:

- `test_create_tablet_hx_modal_load` ? functional consolidation.
- `test_assign_vehicle_audits_event` ? functional consolidation.
- `test_tablet_list_htx_returns_partial_and_direct_reload_renders_full_page` ? presentation.
- `test_historical_storage_never_adds_list_badges` ? presentation.
- `test_historical_storage_never_appears_in_details` ? presentation.
- `test_stale_installation_explains_automatic_recovery` ? presentation.

`apps/tablets/tests/test_stage21_detail_hierarchy.py`:

- `test_detail_separates_asset_and_installation_sections` ? presentation.
- `test_list_has_no_stale_or_pending_state` ? presentation.

`apps/tablets/tests/test_stage21_transfer.py`:

- `test_assign_view_uses_transfer_wording_when_assigned` ? presentation.

`apps/tablets/tests/test_stage2_api_activity.py`:

- `test_activity_partial_polls_every_three_seconds` ? presentation.

`apps/tablets/tests/test_stage2_list.py`:

- `test_list_renders_single_results_table` ? presentation.
- `test_list_shows_count_detail_link_and_contextual_actions` ? presentation.

`apps/tablets/tests/test_views.py`:

- `test_department_admin_can_view_tablet_list_and_detail` ? functional consolidation.
- `test_station_admin_is_denied_tablet_pages` ? functional consolidation.
- `test_department_admin_is_denied_cross_department_tablets` ? functional consolidation.

### Mixed tests simplified
The following **145 retained functions** lost presentation assertions or moved output assertions to structured server data. Some are parametrized. Security-specific non-disclosure/escaping assertions remain. The server-state-label case also drops display copy while retaining the state code and revision. Pending MFA enrollment now verifies an unconfirmed device, disabled MFA and an anonymous session, rather than the presence of a QR image.

`apps/accounts/tests/test_mfa_enrollment.py`:

- `test_mfa_enrollment_creates_pending_device_without_authenticating`

`apps/accounts/tests/test_authentication_integration.py`:

- `test_htmx_reauthentication_uses_full_page_redirect_and_explicit_form_action`
- `test_invalid_htmx_reauthentication_otp_stays_on_the_reauthentication_page`

`apps/file_library/tests/test_explorer_ui.py`:

- `test_same_parent_explorer_navigation_and_full_operations`
- `test_search_sort_pagination_and_breadcrumbs_are_shared`
- `test_action_errors_preserve_values_and_move_browser`
- `test_temporary_parent_and_shared_explorer_restrict_consume_and_expire`
- `test_station_dialog_bounded_search_fallback_reauth_and_stale_choice`
- `test_library_rename_overview_form_and_authorization`
- `test_temporary_access_cannot_rename_library`
- `test_move_destination_pagination_and_mutation`

`apps/file_library/tests/test_libraries.py`:

- `test_portal_bulk_errors_preview_search_and_confirmation`
- `test_data_hub_role_tiles`

`apps/file_library/tests/test_management_ux.py`:

- `test_quota_settings_two_groups_preserve_arbitrary_bytes`
- `test_temporary_selector_bounded_search_and_normal_form_errors`
- `test_permanent_removal_audit_safe_context_and_boundaries`
- `test_historical_audit_context_allowlist_never_renders_raw_metadata`

`apps/file_library/tests/test_publication.py`:

- `test_scheduled_file_publication_detail_offers_build_now`
- `test_elevated_deletion_preserves_published_content`
- `test_publication_permissions_and_ui`
- `test_both_file_scopes_deliver_signed_individual_pdfs_and_authoritative_empty`

`apps/file_library/tests/test_stage4_quotas.py`:

- `test_quota_settings_require_department_scope_reauth_and_show_shortcut`
- `test_concurrent_upload_and_quota_update_serialize`

`apps/file_library/tests/test_temporary_access.py`:

- `test_elevated_permission_boundary_and_read_only_ui`

`apps/ingestion/tests/test_dangerous_goods_ui.py`:

- `test_single_resource_page_is_scoped_and_hides_audit_and_publication_details`
- `test_replace_preview_apply_noop_and_stale_are_safe`
- `test_inspection_searches_variants_aliases_and_renders_tablet_fields`
- `test_system_publications_and_scope_detail_authorization`
- `test_goods_routes_and_department_admin_denied`

`apps/ingestion/tests/test_personnel_home_station_import.py`:

- `test_missing_home_station_skip_uses_shared_review_region_and_excludes_row`
- `test_ambiguous_home_station_requires_same_department_review_choice`

`apps/ingestion/tests/test_phonebook_import.py`:

- `test_phonebook_preview_has_no_coordinate_requirement_and_template_placement`
- `test_phonebook_review_scope_reasons_and_progress_are_resolved_and_stable`

`apps/ingestion/tests/test_review_wizard.py`:

- `test_invalid_coordinate_accept_returns_bound_htmx_review_region`
- `test_valid_coordinate_accept_stages_value_and_advances_without_canonical_mutation`

`apps/ingestion/tests/test_station_vehicle_import.py`:

- `test_missing_station_review_binds_errors_and_does_not_advance`
- `test_ambiguous_station_needs_department_scoped_resolution`
- `test_station_vehicle_import_page_has_csv_template_recent_batches_and_back_link`

`apps/ingestion/tests/test_views.py`:

- `test_hydrant_import_page_offers_only_csv_and_geojson_and_scopes_recent_batches`

`apps/organizations/tests/test_department_presentation.py`:

- `test_locale_time_settings_card_is_authorized_and_preserves_other_policy`

`apps/personnel/tests/test_management_ui.py`:

- `test_personnel_list_is_bounded_active_default_filtered_and_scoped`
- `test_personnel_modal_edit_delete_protection_audit_and_dirtying`

`apps/personnel/tests/test_phase2c_ui.py`:

- `test_create_person_modal_has_bootstrap_fields_and_bound_validation`

`apps/portal/tests/test_administrator_list.py`:

- `test_department_admin_list_is_one_sparse_current_authority_row`
- `test_station_admin_only_row_uses_current_short_code_and_omits_historical_users`
- `test_scope_filter_uses_current_scope_and_department_overrides_station_scope`
- `test_administrator_detail_retains_historical_station_authority`
- `test_pending_administrator_is_visible_scoped_and_cannot_authenticate`
- `test_pending_administrators_participate_in_pagination_without_duplicate_rows`

`apps/portal/tests/test_backlog_phase1_ui.py`:

- `test_phase1_bootstrap_forms_data_hub_and_scoped_htmx_lists`
- `test_phase1_audit_details_are_safe_and_scope_authorized`

`apps/portal/tests/test_data_hub_publication_state.py`:

- `test_data_hub_shows_authoritative_current_publication`
- `test_data_hub_shows_scheduled_update_without_promoting_candidate`
- `test_data_hub_shows_building_update_without_promoting_candidate`
- `test_data_hub_shows_failed_update_without_replacing_current`
- `test_data_hub_shows_not_published_without_an_active_publication`
- `test_data_hub_publication_state_is_department_scoped`
- `test_data_hub_counts_and_access_remain_department_scoped`
- `test_system_goods_state_and_read_only_department_card`

`apps/portal/tests/test_department_outbound_email.py`:

- `test_email_is_one_settings_card_not_navigation_and_mode_is_adaptive`
- `test_one_apply_preserves_blank_password_sets_domains_and_verifies`

`apps/portal/tests/test_navigation.py`:

- `test_system_admin_nav_has_no_department_operational_links`
- `test_department_admin_nav_is_department_wide_without_mode_selector`
- `test_station_admin_nav_resolves_single_station`
- `test_system_admin_not_demoted_by_department_membership`

`apps/portal/tests/test_overview.py`:

- `test_overview_and_topbar_share_cached_role_scoped_attention`

`apps/portal/tests/test_quota_settings.py`:

- `test_quota_form_uses_decimal_defaults`
- `test_invalid_unit_has_labeled_feedback_and_preserves_amount`

`apps/portal/tests/test_stage4_department_administration.py`:

- `test_station_overview_is_fixed_to_its_authorized_station`
- `test_department_settings_are_validated_scoped_and_audited`
- `test_department_audit_is_bounded_safe_and_department_scoped`
- `test_department_accounts_are_bounded_and_mutations_require_reauth`
- `test_administrator_provision_modal_fragments_and_role_assignment`
- `test_provision_duplicate_email_is_a_field_error_without_authority_changes`

`apps/portal/tests/test_stage5_system_administration.py`:

- `test_system_departments_is_bounded_deterministic_filterable_and_system_only`
- `test_api_compatibility_is_structured_modal_validated_audited_and_system_only`
- `test_system_audit_is_bounded_newest_first_filtered_safe_and_system_only`
- `test_system_settings_only_exposes_existing_supported_policies_and_is_system_only`
- `test_system_data_hub_vehicle_rescue_guides_is_system_only_and_uses_audited_service`
- `test_department_data_hub_renders_vehicle_rescue_guides_as_read_only_system_configuration`

`apps/portal/tests/test_station_vehicle_management.py`:

- `test_station_list_is_bounded_ordered_paginated_and_scoped`
- `test_station_edit_modal_validation_success_audit_and_department_boundary`
- `test_station_delete_confirmation_success_and_protected_rollback`
- `test_vehicle_create_modal_validation_success_audit_and_department_boundary`
- `test_vehicle_edit_modal_validation_success_audit_and_department_boundary`
- `test_vehicle_retirement_ends_assignment_and_preserves_tablet`
- `test_station_detail_vehicle_actions_legacy_redirect_and_navigation`

`apps/portal/tests/test_system_outbound_email.py`:

- `test_outbound_email_is_system_admin_only_and_is_listed_in_system_settings`
- `test_credentials_are_write_only_and_replace_clear_remain_audited`
- `test_system_admin_can_configure_the_https_egress_proxy`
- `test_verification_is_non_delivery_service_action_and_presents_sanitized_status`
- `test_invalid_or_incomplete_configuration_is_safe_form_feedback`
- `test_managed_mail_eligibility_is_on_the_system_department_detail`

`apps/publications/tests/test_fire_plan_v2_delivery.py`:

- `test_incomplete_historical_generation_never_activates`

`apps/publications/tests/test_history_queries.py`:

- `test_history_payload_scaling`

`apps/publications/tests/test_publications_ui.py`:

- `test_primary_list_is_one_row_per_scope_with_current_version_and_detail_link`
- `test_list_and_building_row_use_stored_fingerprints_without_rebuilding_source`
- `test_staged_action_modal_is_scope_authorized_and_explains_history`
- `test_cancel_modal_reports_a_build_that_finished_first`
- `test_scope_detail_has_bounded_history_and_detail_only_successful_delete_actions`
- `test_filters_are_server_side_and_count_scopes_not_attempts`
- `test_cancelled_attempt_leaves_canonical_change_dirty_and_manual_stage_uses_new_version`
- `test_reverting_to_active_source_becomes_clean_without_reusing_cancelled_attempt`
- `test_inspect_changes_uses_the_latest_coalesced_staged_snapshot`
- `test_shared_detail_rebuild_for_registered_scopes`
- `test_current_rebuild_confirmation_and_duplicate_rejection`

`apps/publications/tests/test_scope_filters.py`:

- `test_scope_then_status_filters`
- `test_default_reset_and_accessible_filter_panel`
- `test_station_and_overall_resets_preserve_their_distinct_scope`
- `test_station_pagination_retains_uuid_and_status`
- `test_invalid_station_never_falls_back_to_all`
- `test_empty_states_preserve_scope_when_clearing_status`
- `test_pagination_htmx_status_and_history_restore`
- `test_station_search_bounded_fallback_and_authority`
- `test_all_stations_without_selection`

`apps/publications/tests/test_state.py`:

- `test_scope_operational_states_derives_per_scope_state`

`apps/reference_data/tests/test_document_management_ui.py`:

- `test_fire_plan_ui_edit_lifecycle_delete_and_publication`
- `test_klgv_ui_edit_lifecycle_delete_and_scope`

`apps/reference_data/tests/test_fire_plan_view.py`:

- `test_fire_plan_add_form_exposes_only_the_canonical_identity_fields`

`apps/reference_data/tests/test_hydrant_management_ui.py`:

- `test_hydrant_list_is_bounded_active_by_default_and_filterable`
- `test_hydrant_modal_lifecycle_delete_publication_and_scope`

`apps/reference_data/tests/test_phonebook.py`:

- `test_phonebook_list_and_crud_presentation`
- `test_phonebook_manual_station_binding_and_list_filters`
- `test_phonebook_modal_create_uses_fragment_success_and_non_htmx_fallback`
- `test_phonebook_invalid_htmx_create_stays_in_bound_modal`
- `test_manual_phonebook_duplicate_create_transitions_to_dedicated_review_and_updates`
- `test_phonebook_pagination_preserves_filters_and_resets_for_live_filtering`

`apps/report_transfer/tests/test_portal.py`:

- `test_scanner_get_then_exactly_one_login_and_used_revoke`
- `test_token_sixty_minute_boundary`
- `test_pdf_jpeg_preview_and_retention`
- `test_multiple_reports_all_blocks_safe_html_and_conflicting_replay`
- `test_used_token_revoke_does_not_depend_on_remaining_recipient_eligibility`
- `test_partial_expiry_does_not_hide_remaining_selected_reports`

`apps/tablets/tests/test_asset_number_generation.py`:

- `test_registration_ui_uses_department_policy_without_consuming_preview`
- `test_registration_ui_manual_field_when_automatic_numbering_is_disabled`
- `test_preview_changes_with_policy_but_does_not_reserve_the_candidate`
- `test_post_after_preview_allocates_current_candidate_not_the_stale_preview`

`apps/tablets/tests/test_management_ui.py`:

- `test_assign_vehicle_same_department`
- `test_adoption_get_renders_confirmation_without_token`
- `test_adoption_post_creates_invitation_and_shows_token_once`
- `test_adoption_status_polling_states`

`apps/tablets/tests/test_stage2_list.py`:

- `test_foreign_station_and_vehicle_filters_cannot_leak_other_department_data`
- `test_pagination_preserves_filters`

`apps/tablets/tests/test_stage2_origin.py`:

- `test_list_origin_returns_to_list_and_preserves_query`

`tools/fake_ipad/test_reports.py`:

- `test_fixed_five_snapshot_later_upload_replay_consumed_token_and_replacement`

### Bounded validation results (2026-10-29)

All actual test runs used isolated local PostgreSQL/PostGIS, disposable files and
mocked mail/in-process or mocked networking. No live mail, testserver writes,
production changes, Git writes or adopted Fake-iPad/dataset modifications.

| Selection | Result | Seconds | Interpretation |
|---|---|---:|---|
| Portal/tablet baseline, `-x` | 97 passed, 1 failed | 19.31 | Stops at known SYSTEM Data Hub expectation; not a complete group timing. |
| Initial seven failure-family modules | 132 passed, 14 failed | 157.71 | Reproduces 13 historical failures plus the same empty-scope assumption in cancellation. |
| Portal/tablets + quota/hydrant upgrade validation | 407 passed, 5 failed | 119.33 | Four context-conversion mistakes and historical job-column mismatch; corrected and covered below. |
| Seven failure-family modules after fixes | 143 passed, 3 failed | 157.44 | Remaining three were an incorrect SYSTEM-panel expectation introduced during conversion, corrected in the 44-case follow-up. No runtime gain claimed for this group. |
| Other affected modules (explicit list below) | 588 passed, 2 failed | 529.32 | OTP uses failure audit rather than form errors; storm result is a dict containing `reports`. Both assertion-conversion mistakes corrected and rerun successfully. This expensive group was not broadly repeated. |
| Final correction/strengthening selection | **44 passed** | **35.03** | Includes both above failures, all read-path cases, administrator history, phonebook form errors, asset previews, station-import scope, cancellation, unchanged ingestion scopes, and the five-report PDF/JPEG snapshot. |
| Shared migration fixture + security/integration | **217 passed** | **124.64** | Existing migration upgrades, report authorization/audit, PDF-Mail admission/delivery/idempotency, independent nine-dataset Fake-iPad generation verification. |
| Tablet + publication-state + phonebook follow-up | **36 passed** | **21.34** | Covers latest consolidations, code-based state and reconciliation assertions. |
| Final tablet measurement | **32 passed** | **14.67** | Remaining cases in the same original four-module universe (two pure modules are deleted). |
| Pending MFA enrollment | **1 passed** | **12.69** | Device/session state instead of QR image appearance. |
| Final full collection only | **1,998 collected** | **1.89** | Rechecked after the MFA assertion conversion. No full execution and no claim of a green complete suite. |
| Routine collection only | **1,996 collected, 2 deselected** | **1.87** | Only the existing extended workloads excluded; no routine execution claim. |

No skips occurred in these bounded execution groups. The retained platform
requirements above were not certified by a complete Linux run. All **22 known
failures are resolved in targeted coverage; zero known functional failures remain
open**. This is a targeted result, not a proof that the complete suite is green.
The intermediate conversion failures are recorded rather than omitted.

### Measurements with comparable selections

| Group | Before | After | Measured difference |
|---|---|---|---|
| Same 14 wizard cases, same assertions, only transaction mode changed | 14 passed / 46.69 s | 14 passed / 13.03 s | **33.66 s / 72.1% less wall time** |
| Original tablet list/template/detail/management module universe | 53 passed / 15.10 s | 32 passed / 14.67 s | **0.43 s / 2.8% less wall time**, 21 fewer cases |

Wizard database initialization was 11.17 s before and 11.06 s after. Its old
per-test flush teardown was about 2.3?2.6 s; rollback teardown is at most 0.09 s
in the reported slowest durations. No concurrency/commit behavior was mocked.
Tablet database initialization dominates both runs (11.55/11.62 s); removing
cheap markup checks does **not** explain a large speed gain. The 50/400-scope
read-path checks remain small (the summary case was 0.10 s in final corrections).
Migration database setup remains around 4?10 s per case, with one observed
20.03-second hydrant database teardown. No production database privileges were
changed to accelerate these costs. Further broad reruns were avoided.

### Exact commands and selections

Every command below uses `.venv/Scripts/python.exe -m pytest
--ds=config.settings.test -p no:cacheprovider`, with `-q --tb=short` and durations
as shown. These are bounded selections, not a routine allowlist.

```powershell
# Failure families, before and after (same seven modules):
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/ingestion/tests/test_services.py apps/publications/tests/test_history_queries.py apps/publications/tests/test_retention_hardening.py apps/publications/tests/test_read_path_performance.py apps/publications/tests/test_debounce.py apps/publications/tests/test_wake.py apps/file_library/tests/test_publication_reverts.py -q --tb=short --durations=10

# Wizard before/after: identical module and assertions; only django_db mode differs.
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/ingestion/tests/test_review_wizard.py -q --durations=10 --tb=short

# Security/integration regressions:
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/publications/tests/test_document_migrations.py apps/publications/tests/test_bulk_intent_migration.py apps/publications/tests/test_scope_migration_upgrade.py apps/report_transfer/tests/test_authorization_audit.py apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py tools/fake_ipad/test_generations.py -q --tb=short --durations=10 -rs

# Final correction selection:
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/accounts/tests/test_authentication_integration.py apps/publications/tests/test_read_path_performance.py apps/tablets/tests/test_asset_number_generation.py apps/portal/tests/test_administrator_list.py::test_administrator_detail_retains_historical_station_authority apps/reference_data/tests/test_phonebook.py::test_phonebook_invalid_htmx_create_stays_in_bound_modal apps/ingestion/tests/test_station_vehicle_import.py::test_station_vehicle_import_page_has_csv_template_recent_batches_and_back_link apps/publications/tests/test_publications_ui.py::test_cancel_modal_reports_a_build_that_finished_first apps/ingestion/tests/test_services.py::test_preview_is_side_effect_free_and_apply_dirties_one_scope apps/ingestion/tests/test_services.py::test_invalid_preview_has_no_staged_canonical_side_effects apps/ingestion/tests/test_services.py::test_cancelled_preview_never_mutates_canonical_data_or_publication_state tools/fake_ipad/test_reports.py::test_fixed_five_snapshot_later_upload_replay_consumed_token_and_replacement -q --tb=short --durations=10

# Original tablet measurement additionally selected the two now-deleted pure modules:
# apps/tablets/tests/test_list_template_regressions.py
# apps/tablets/tests/test_stage21_detail_hierarchy.py
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/tablets/tests/test_stage2_list.py apps/tablets/tests/test_management_ui.py -q --durations=10 --tb=short

# Other affected modules used for the 590-case execution:
$affected = @(
  'apps/accounts/tests/test_authentication_integration.py'
  'apps/file_library/tests/test_backend_preparation.py'
  'apps/file_library/tests/test_explorer_ui.py'
  'apps/file_library/tests/test_libraries.py'
  'apps/file_library/tests/test_management_ux.py'
  'apps/file_library/tests/test_publication.py'
  'apps/file_library/tests/test_stage4_quotas.py'
  'apps/file_library/tests/test_temporary_access.py'
  'apps/ingestion/tests/test_dangerous_goods_ui.py'
  'apps/ingestion/tests/test_personnel_home_station_import.py'
  'apps/ingestion/tests/test_phonebook_import.py'
  'apps/ingestion/tests/test_station_vehicle_import.py'
  'apps/ingestion/tests/test_views.py'
  'apps/organizations/tests/test_department_presentation.py'
  'apps/personnel/tests/test_management_ui.py'
  'apps/personnel/tests/test_phase2c_ui.py'
  'apps/portal/tests/test_administrator_list.py'
  'apps/portal/tests/test_backlog_phase1_ui.py'
  'apps/portal/tests/test_data_hub_publication_state.py'
  'apps/portal/tests/test_department_outbound_email.py'
  'apps/portal/tests/test_navigation.py'
  'apps/portal/tests/test_overview.py'
  'apps/portal/tests/test_quota_settings.py'
  'apps/portal/tests/test_stage4_department_administration.py'
  'apps/portal/tests/test_stage5_system_administration.py'
  'apps/portal/tests/test_station_vehicle_management.py'
  'apps/portal/tests/test_system_outbound_email.py'
  'apps/publications/tests/test_fire_plan_v2_delivery.py'
  'apps/publications/tests/test_publications_ui.py'
  'apps/publications/tests/test_read_path_performance.py'
  'apps/publications/tests/test_retention.py'
  'apps/publications/tests/test_scope_filters.py'
  'apps/reference_data/tests/test_document_management_ui.py'
  'apps/reference_data/tests/test_fire_plan_view.py'
  'apps/reference_data/tests/test_hydrant_location_migration.py'
  'apps/reference_data/tests/test_hydrant_management_ui.py'
  'apps/reference_data/tests/test_phonebook.py'
  'apps/report_transfer/tests/test_portal.py'
  'apps/tablets/tests/test_asset_number_generation.py'
  'apps/tablets/tests/test_management_ui.py'
  'apps/tablets/tests/test_stage21_transfer.py'
  'apps/tablets/tests/test_stage22_migration.py'
  'apps/tablets/tests/test_stage2_api_activity.py'
  'apps/tablets/tests/test_stage2_list.py'
  'apps/tablets/tests/test_stage2_origin.py'
  'apps/tablets/tests/test_views.py'
  'tools/fake_ipad/test_reports.py'

)
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider @affected -q --tb=short --durations=15 -rs
```

The current list contains fewer cases after the final duplicate deletions; the
590-case result describes the intermediate working tree, with its two failing
cases subsequently passing in the explicit correction selection. The complete
collection inventory above describes the final working tree.

Django system checks and migration drift checks pass. Ruff lint across
`apps config tools conftest.py`, formatting for every modified Python file and
read-only Git whitespace checks pass. Production files were not reformatted.
The repository's complete CI command remains available for the later release
checkpoint; it was deliberately not run here.
