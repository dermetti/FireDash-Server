| Replacement | Original range (inclusive, existing files only) |
| --- | --- |
| `accounts.0001_squashed_0002_beta` | `0001_initial` through `0002_alter_user_groups_accountsetuptoken_and_more` |
| `assignments.0001_squashed_0002_beta` | `0001_initial` through `0002_tablet_assignment_end_reason` |
| `audit.0001_squashed_0004_beta` | `0001_initial` through `0004_restore_audit_truncate_immutability` |
| `authorization.0001_squashed_0007_beta` | `0001_initial` through `0007_department_managed_mail_eligibility` |
| `file_library.0001_squashed_0010_beta` | `0001_initial` through `0010_publication_capacity_reviews` |
| `ingestion.0001_squashed_0006_beta` | `0001_workstream4_canonical_ingestion` through `0006_clear_obsolete_goods_manifests` |
| `organizations.0001_squashed_0007_beta` | `0001_initial` through `0007_department_locale_and_timezone` |
| `outbound_mail.0001_squashed_0006_beta` | `0001_initial` through `0006_systemmailconfiguration_outbound_mail_https_proxy` |
| `personnel.0001_squashed_0002_beta` | `0001_initial` through `0002_personnelretentionpolicy_person_anonymized_at_and_more` |
| `publications.0001_squashed_0011_beta` | `0001_initial` through `0011_department_klgv_plans_registry` |
| `publications.0012_squashed_0030_beta` | `0012_publication_lifecycle_states` through `0030_correct_legacy_bulk_rebuild_intent` |
| `publications.0031_squashed_0033_beta` | `0031_unified_document_generation` through `0033_strict_document_generation_metadata` |
| `reference_data.0001_squashed_0015_beta` | `0001_initial` through `0015_remove_retired_hydrant_preview` |
| `report_transfer.0001_squashed_0003_beta` | `0001_initial` through `0003_portal_transfers` |
| `tablets.0001_squashed_0002_beta` | `0001_initial` through `0002_tablet_asset_number_tablet_created_by_and_more` |
| `tablets.0003_squashed_0009_beta` | `0003_app_version_tracking_and_completion_recovery` through `0009_appinstallation_storage_telemetry` |

| Replacement | Dependencies |
| --- | --- |
| `accounts.0001_squashed_0002_beta` | `auth.0012_alter_user_first_name_max_length` |
| `assignments.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `personnel.0001_squashed_0002_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |
| `audit.0001_squashed_0004_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `authorization.0001_squashed_0007_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `file_library.0001_squashed_0010_beta` | `audit.0001_squashed_0004_beta`, `organizations.0001_squashed_0007_beta`, `publications.0012_squashed_0030_beta`, `AUTH_USER_MODEL` |
| `ingestion.0001_squashed_0006_beta` | `audit.0001_squashed_0004_beta`, `file_library.0001_squashed_0010_beta`, `organizations.0001_squashed_0007_beta`, `publications.0012_squashed_0030_beta`, `AUTH_USER_MODEL` |
| `organizations.0001_squashed_0007_beta` | `AUTH_USER_MODEL` |
| `outbound_mail.0001_squashed_0006_beta` | `organizations.0001_squashed_0007_beta`, `tablets.0003_squashed_0009_beta`, `AUTH_USER_MODEL` |
| `personnel.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `publications.0001_squashed_0011_beta` | `organizations.0001_squashed_0007_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |
| `publications.0012_squashed_0030_beta` | `audit.0001_squashed_0004_beta`, `publications.0001_squashed_0011_beta`, `reference_data.0001_squashed_0015_beta`, `tablets.0003_squashed_0009_beta` |
| `publications.0031_squashed_0033_beta` | `file_library.0001_squashed_0010_beta`, `publications.0012_squashed_0030_beta` |
| `reference_data.0001_squashed_0015_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `report_transfer.0001_squashed_0003_beta` | `organizations.0001_squashed_0007_beta`, `personnel.0001_squashed_0002_beta`, `tablets.0003_squashed_0009_beta` |
| `tablets.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `tablets.0003_squashed_0009_beta` | `assignments.0001_squashed_0002_beta`, `organizations.0001_squashed_0007_beta`, `publications.0001_squashed_0011_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |
