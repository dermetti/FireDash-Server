# Retired Phase 1 migration files

This is the exact deletion inventory for Phase 2: 103 original files plus the Phase 1 `reference_data.0015` cleanup (104 total). All 16 approved beta files are retained.

| App | Files retired |
| --- | --- |
| `accounts` | 2 |
| `assignments` | 2 |
| `audit` | 4 |
| `authorization` | 7 |
| `file_library` | 10 |
| `ingestion` | 6 |
| `organizations` | 7 |
| `outbound_mail` | 6 |
| `personnel` | 2 |
| `publications` | 32 |
| `reference_data` | 14 |
| `report_transfer` | 3 |
| `tablets` | 9 |

## Exact files

- `apps/accounts/migrations/0001_initial.py`
- `apps/accounts/migrations/0002_alter_user_groups_accountsetuptoken_and_more.py`
- `apps/assignments/migrations/0001_initial.py`
- `apps/assignments/migrations/0002_tablet_assignment_end_reason.py`
- `apps/audit/migrations/0001_initial.py`
- `apps/audit/migrations/0002_immutable_audit_event.py`
- `apps/audit/migrations/0003_allow_test_database_flush.py`
- `apps/audit/migrations/0004_restore_audit_truncate_immutability.py`
- `apps/authorization/migrations/0001_initial.py`
- `apps/authorization/migrations/0002_single_department_admin_constraint.py`
- `apps/authorization/migrations/0003_api_version_compatibility_policy.py`
- `apps/authorization/migrations/0004_authority_lifecycle_status.py`
- `apps/authorization/migrations/0005_reconcile_lifecycle_provenance_constraint_state.py`
- `apps/authorization/migrations/0006_vehicle_rescue_guides_configuration.py`
- `apps/authorization/migrations/0007_department_managed_mail_eligibility.py`
- `apps/file_library/migrations/0001_initial.py`
- `apps/file_library/migrations/0002_integrity_guards.py`
- `apps/file_library/migrations/0003_publicationcontent.py`
- `apps/file_library/migrations/0004_publication_guards.py`
- `apps/file_library/migrations/0005_temporarystationaccess.py`
- `apps/file_library/migrations/0006_remove_library_capacity.py`
- `apps/file_library/migrations/0007_storageimpact_capacityreview.py`
- `apps/file_library/migrations/0008_department_quota_policy.py`
- `apps/file_library/migrations/0009_decimal_quota_defaults.py`
- `apps/file_library/migrations/0010_publication_capacity_reviews.py`
- `apps/ingestion/migrations/0001_workstream4_canonical_ingestion.py`
- `apps/ingestion/migrations/0002_alter_importbatch_domain.py`
- `apps/ingestion/migrations/0003_alter_importbatch_domain.py`
- `apps/ingestion/migrations/0004_dangerous_goods_import_domain.py`
- `apps/ingestion/migrations/0005_system_dangerous_goods.py`
- `apps/ingestion/migrations/0006_clear_obsolete_goods_manifests.py`
- `apps/organizations/migrations/0001_initial.py`
- `apps/organizations/migrations/0002_vehicle.py`
- `apps/organizations/migrations/0003_structured_station_address.py`
- `apps/organizations/migrations/0004_remove_station_address.py`
- `apps/organizations/migrations/0005_department_tablet_lease_days.py`
- `apps/organizations/migrations/0006_department_tablet_asset_number_policy.py`
- `apps/organizations/migrations/0007_department_locale_and_timezone.py`
- `apps/outbound_mail/migrations/0001_initial.py`
- `apps/outbound_mail/migrations/0002_systemmailconfiguration_last_verification_code_and_more.py`
- `apps/outbound_mail/migrations/0003_departmentrecipientpolicy_and_more.py`
- `apps/outbound_mail/migrations/0004_departmentmailconfiguration_last_smtp_verification_code_and_more.py`
- `apps/outbound_mail/migrations/0005_tabletreportdeliveryrequest.py`
- `apps/outbound_mail/migrations/0006_systemmailconfiguration_outbound_mail_https_proxy.py`
- `apps/personnel/migrations/0001_initial.py`
- `apps/personnel/migrations/0002_personnelretentionpolicy_person_anonymized_at_and_more.py`
- `apps/publications/migrations/0001_initial.py`
- `apps/publications/migrations/0002_phase6_registry_features.py`
- `apps/publications/migrations/0003_phase7_artifacts.py`
- `apps/publications/migrations/0004_phase7_closeout_integrity.py`
- `apps/publications/migrations/0005_dataset_key_grant.py`
- `apps/publications/migrations/0006_signed_manifest.py`
- `apps/publications/migrations/0007_datasetpublication_artifact_signing_key_version.py`
- `apps/publications/migrations/0008_publication_job_debounce.py`
- `apps/publications/migrations/0010_publicationjob_bulk_trigger.py`
- `apps/publications/migrations/0011_department_klgv_plans_registry.py`
- `apps/publications/migrations/0012_publication_lifecycle_states.py`
- `apps/publications/migrations/0013_datasetpublication_source_fingerprint.py`
- `apps/publications/migrations/0014_datasetpublication_source_snapshot.py`
- `apps/publications/migrations/0015_current_source_fingerprint.py`
- `apps/publications/migrations/0016_publication_retention.py`
- `apps/publications/migrations/0017_fire_plan_document_artifacts.py`
- `apps/publications/migrations/0018_fire_plan_v2_delivery.py`
- `apps/publications/migrations/0019_fire_plan_document_artifact_cleanup.py`
- `apps/publications/migrations/0020_fire_plan_document_manifest_cutover.py`
- `apps/publications/migrations/0021_klgv_document_manifest_v2.py`
- `apps/publications/migrations/0022_klgv_document_v2_ready_guard.py`
- `apps/publications/migrations/0023_phonebook_dataset_scope_registry.py`
- `apps/publications/migrations/0024_dangerous_goods_source_revisions.py`
- `apps/publications/migrations/0025_promote_klgv_document_v2_registry.py`
- `apps/publications/migrations/0026_rename_publications_scope_s_631ca6_idx_pub_src_scope_rev_idx.py`
- `apps/publications/migrations/0027_publication_scope_types.py`
- `apps/publications/migrations/0028_file_library_registry.py`
- `apps/publications/migrations/0029_publicationjob_allow_clean_rebuild.py`
- `apps/publications/migrations/0030_correct_legacy_bulk_rebuild_intent.py`
- `apps/publications/migrations/0031_unified_document_generation.py`
- `apps/publications/migrations/0032_remove_legacy_fire_plan_delivery.py`
- `apps/publications/migrations/0033_strict_document_generation_metadata.py`
- `apps/reference_data/migrations/0001_initial.py`
- `apps/reference_data/migrations/0002_rename_reference_d_departm_3ca664_idx_reference_d_departm_f01f3d_idx_and_more.py`
- `apps/reference_data/migrations/0004_hydrant_enum_diameter.py`
- `apps/reference_data/migrations/0005_remove_hydrant_reference_d_departm_8bffe7_idx_and_more.py`
- `apps/reference_data/migrations/0006_workstream4_canonical_ingestion.py`
- `apps/reference_data/migrations/0007_fire_plan_location_metadata.py`
- `apps/reference_data/migrations/0008_align_fire_plan_identity.py`
- `apps/reference_data/migrations/0009_fire_plan_operational_locations.py`
- `apps/reference_data/migrations/0010_phase2_hydrant_address.py`
- `apps/reference_data/migrations/0011_phase2_klgv_canonical_metadata.py`
- `apps/reference_data/migrations/0012_finalize_klgv_required_metadata.py`
- `apps/reference_data/migrations/0013_hydrant_descriptive_location.py`
- `apps/reference_data/migrations/0014_phonebook_entries.py`
- `apps/reference_data/migrations/0015_remove_retired_hydrant_preview.py`
- `apps/report_transfer/migrations/0001_initial.py`
- `apps/report_transfer/migrations/0002_report_document_integrity.py`
- `apps/report_transfer/migrations/0003_portal_transfers.py`
- `apps/tablets/migrations/0001_initial.py`
- `apps/tablets/migrations/0002_tablet_asset_number_tablet_created_by_and_more.py`
- `apps/tablets/migrations/0003_app_version_tracking_and_completion_recovery.py`
- `apps/tablets/migrations/0004_tabletapiactivity_alter_tablet_display_name_and_more.py`
- `apps/tablets/migrations/0005_alter_tablet_status.py`
- `apps/tablets/migrations/0006_remove_removed_tablet_data.py`
- `apps/tablets/migrations/0007_enforce_current_tablet_asset_states.py`
- `apps/tablets/migrations/0008_remove_obsolete_reactivation_flow.py`
- `apps/tablets/migrations/0009_appinstallation_storage_telemetry.py`
