# Phase 1 test refactor

Only the approved test modules and their local fixture were changed. Production
code, database roles, execution profiles, and Git refs/index were not changed.

## Case mapping

Each numbered row accounts for one fewer collected case. Names below are pytest
function names without the `test_` prefix, relative to the stated test directory.

### Redundant cases (10)

| # | Removed case | Retained coverage / destination |
|---|---|---|
| 1 | tablets: `stage2_replacement::replacement_invitation_keeps_current_installation_active` | `stage21_reprovision::reprovision_keeps_tablet_identity_and_assignment`: invitation tablet ID and active previous installation before completion. |
| 2 | tablets: `stage2_replacement::replacement_adoption_activates_new_and_replaces_previous` | Same reprovision scenario: new ACTIVE, previous REPLACED, exactly one active installation. |
| 3 | tablets: `stage2_replacement::replacement_preserves_logical_tablet` | Same reprovision scenario: original tablet PK exists, one departmental tablet, two installations, assignment preserved. |
| 4 | tablets: `stage2_replacement::replaced_installation_drives_purge_flag` | Same reprovision scenario checks the stronger exact REPLACED status; actual HTTP purge flags remain in unchanged `test_api.py`. |
| 5 | tablets: `stage21_reprovision::reprovision_user_facing_wording` | `stage21_detail_hierarchy::detail_separates_asset_and_installation_sections`: required reprovision wording and absent replacement wording. |
| 6 | tablets: `stage21_stale::stale_health_derives_from_lease_expiry` | `lifecycle::mark_stale_installations_transitions_expired_leases`: transition count, STALE status, stale timestamp, and expired authorization lease. |
| 7 | tablets: `stage2_list::status_filter_exposes_historical_rows` | Existing `default_hides_lost_and_retired_tablets_until_an_asset_state_is_selected`: exact default visibility plus separate LOST and RETIRED filtered responses. |
| 8 | ingestion: `limits::geojson_10001_features_are_accepted` | Retained 50,000 accepted, 50,001 rejected, and byte-size limit. |
| 9 | ingestion: `limits::geojson_38000_features_are_accepted` | Same retained parser boundaries. |
| 10 | publications: `summary_validation::large_hydrant_department_builds_summary[38000]` | Retained `[50000]` builder case checks active count and status bucket count; all summary validation boundaries retained. |

### Consolidated executions (10)

| # | Eliminated separate execution | Combined destination and preserved assertions |
|---|---|---|
| 11 | tablets: `stage2_assignment::station_admin_assignment_is_audited` | `station_admin_intra_station_move_succeeds`: original two vehicles and reassignment, current vehicle, audit action and station-admin actor. Separate denial tests unchanged. |
| 12 | tablets: `stage2_identity::same_asset_number_different_departments_permitted` | `same_display_name_and_asset_number_different_departments_permitted`: original display-name pair plus separate original asset-number pair, both departments' actors, resulting department and asset number. Same-tenant rejection and database transaction tests unchanged. |
| 13 | tablets: `stage2_origin::list_origin_shows_feedback_on_list` | `list_origin_returns_to_list_and_preserves_query`: original POST, 302, exact query-preserving URL, subsequent GET and feedback. External redirect rejection unchanged. |
| 14 | tablets: `stage21_detail_hierarchy::reprovision_appears_under_installation_actions` | `detail_separates_asset_and_installation_sections`: same adopted tablet rendering, all section and action labels. |
| 15 | tablets: `stage21_detail_hierarchy::no_forbidden_user_facing_wording` | Same rendering: all five forbidden strings remain absent. List-state rendering stays separate. |
| 16 | publications: `fire_plan_manifest::fire_plan_without_coordinates_publishes_null` | Three original missing-field scenarios become `fire_plan_missing_fields_publish_null_per_document`; distinct persisted documents with unique nonblank identifiers and independent output lookup by ID. Both coordinates must be null on the coordinate-less document. |
| 17 | publications: `fire_plan_manifest::fire_plan_without_operational_locations_publishes_null` | Same multi-document manifest: all three operational fields null on their document; identifier-less document independently checks null identifier and preserved Unicode address. Exact document ID set and populated coordinates/identifiers prevent output conflation. Transaction marker retained. |
| 18 | ingestion: `hydrant_preview_scaling::single_preview_baseline_is_scoped_to_imported_identifier` | `single_preview_query_count_is_bounded[3000]`: at most 25 queries, exact baseline identifier, update count and summary identifier. |
| 19 | ingestion: separate preview-side-effect and confirmation executions | `preview_has_no_publication_side_effects_until_confirmed`: no dirty hydrant scope or publication jobs after preview; after applying that batch, correct scope revision 1 and exactly one job. |
| 20 | portal: `phonebook_data_hub::phonebook_and_personnel_have_distinct_data_hub_icons` | `data_hub_publication_state::data_hub_cards_preserve_navigation_and_distinct_icons`: HTTP 200, Phonebook phone icon, Personnel people icon, original single-link card and absent nested controls/obsolete labels. |

## Fixture population

`hydrant_scaling_context` remains function-scoped and defaults to three fresh
hydrants. Query-boundedness, population reimport convergence, and batch baseline
scaling explicitly request 3,000 rows through indirect parametrization. The
unrelated-mutation test still changes a different existing identifier. Fixture
population across this module drops from 33,000 rows (11 × 3,000) to 9,018
(3 × 3,000 + 6 × 3), without shared mutable database state.

## Validation

Both focused runs use the local virtualenv Python, PostgreSQL/PostGIS test
settings (`DJANGO_SETTINGS_MODULE=config.settings.test`), and identical pytest
options: `-p no:cacheprovider -q --durations=15`. Both create/destroy the test
database normally; no reuse, parallel execution, or custom scratch path.

The focused module set is the 14 modified surviving modules, the removed
Phonebook module on the baseline run, and unchanged `apps/tablets/tests/test_api.py`.
This includes lifecycle/concurrency, HTTP purge behavior, authorization denials,
tenant isolation, parser/summary boundaries, and publication transitions.

| Measurement | Before | After | Reduction |
|---|---:|---:|---:|
| Repository collection only | 1,189 | 1,169 | 20 |
| Focused PostgreSQL cases (all passed) | 186 | 166 | 20 |
| Focused pytest elapsed time | 72.41 s | 51.87 s | 20.54 s (28.4%) |

Collection used `--collect-only -q -p no:cacheprovider` before any edits and
after the refactor. The full repository regression suite was not executed.
Timings are single comparable focused runs, including normal database setup and
teardown, rather than a statistical benchmark. Ruff on every modified surviving
Python module and `git diff --check` also passed. All 20 requested reductions were
achieved; no distinct guarantee required retaining a targeted redundant execution.
Phase 2 was not started.
