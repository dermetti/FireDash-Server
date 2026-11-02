# Phase 3.6.2 ? CI typing and deployment verifier compatibility

The CI inventory was 164 errors in 30 files. The Windows reproduction of the
same `mypy .` command found 167 errors in 32 files; the additional diagnostics
concern POSIX-only APIs absent from Windows type stubs. All were corrected
without changing mypy exclusions, adding mypy ignores or weakening CI gates.

## Typing changes

- Separate RSA public/private variables in the deployment credential helper.
- Pin `types-reportlab==4.5.1.20260827` in development requirements, matching
  ReportLab 4.5.1. Narrow the registered TrueType font to its actual type.
- Annotate JSON boundaries, collections, cleanup IDs and the existing transient
  restore-validation model hint. The hint is not a database field.
- Describe the Fake iPad transport interface structurally so the existing
  independent Django transports and HTTP implementation share the correct type.
- Reuse pytest fixtures by import rather than invoking decorated `__wrapped__`
  functions. Keep their scope, setup and teardown behavior.
- Assert nullable worker/model results before using them in tests. Use Django
  Client's explicit `headers=` argument for typed header dictionaries.
- Reflect the existing `Department | None` authorization input: the implementation
  already denies `None`. Preserve that fail-closed behavior.
- Keep the mail-provider usability check and explicitly guard a missing provider.
  Reject malformed/absent portal session UUIDs before querying, with the existing
  403 result. Add focused malformed-session regression coverage.
- Narrow optional POSIX APIs within the existing POSIX/Unix-only paths. No
  cryptographic, API, authentication, publication or retention contract changes.

## Debian systemd compatibility

`systemctl show` may render complex D-Bus properties, including LoadCredential,
as `[unprintable]`. An empty complex property is also not treated as evidence of
correct configuration. The verifier now reads `systemctl cat -- UNIT` privately
and inspects the fragment and ordered drop-ins. It first requires
`NeedDaemonReload=no`, so stale files cannot substitute for loaded configuration.
It handles list resets, continuation lines and quoted arguments; scalar overrides
use their final value. Unsupported escaping/specifiers fail closed, with no unit
contents or credential values printed.

This validates the canonical credential mapping and rejects missing, conflicting,
duplicate or inappropriately imported report credentials. It also checks spool
write paths, exact expiry argv/environment and the five-minute timer configuration
without depending on the pretty-printing of ReadWritePaths, ExecStart or
TimersMonotonic. Scalar runtime properties remain checked; both plain values and
`Property=value` output are accepted. Missing or opaque scalar values fail.

The existing running-process mount-namespace probe remains mandatory. It proves
that the actual web service can derive the advertised active public key using
its systemd-delivered keyring. No tablet token, report or email is needed.

Deployment helper tests cover printable, `[unprintable]`, empty complex and
property-prefixed output; missing units/configuration; pending daemon reload;
credential conflicts/resets; worker leakage; quoting/continuations; incorrect
commands, environment, write paths and timer intervals; and failed runtime probes.

## Local validation

- `mypy .`: no issues in 410 source files; also green with `--platform linux`.
- `ruff check .`: passed; `ruff format --check .`: 528 files formatted.
- `manage.py check`: no issues. Migration drift check: no changes detected.
- `manage.py spectacular --validate --fail-on-warn`: passed without warnings.
- Both `test_report_transfer_deployment.sh` and
  `test_report_transfer_installer.sh`: passed. The former explicitly skips the
  two native Unix checks described below.
- `bash -n` for the verifier and both Report Transfer shell tests: passed.
  Python byte-compilation of the deployment credential helper: passed.
- ShellCheck is not installed on this Windows host.
- Bounded PostgreSQL/disposable-storage regression: **685 passed, 1 skipped,
  1 failed in 296.22s**. The failure was the mail-provider test double noted
  above; the correction retains the original usability flag as well as explicit
  null checking. The entire affected portal module is rerun after the correction;
  **57 passed in 50.52s**, including all four new malformed-session cases.
  No selected failure remains unresolved. The earlier bounded result is reported
  separately rather than claiming a second green run of the entire selection.

The bounded selection was `tools/fake_ipad`, `tools/test_fake_ipad.py`,
`tools/test_fake_ipad_server_contract.py`, all Report Transfer tests, both
changed dangerous-goods integration/contract modules, publication history and
retention-hardening modules, the complete tablet API module, and these specific
changed regressions:

```text
apps/publications/tests/test_phase4a_lifecycle.py::test_rebuild_cannot_modify_an_existing_attempt
apps/publications/tests/test_fire_plan_v2_delivery.py::test_generic_fire_plan_worker_failure_preserves_current_publication
apps/publications/tests/test_fire_plan_v2_delivery.py::test_document_membership_index_reads_each_artifact_identity_once
apps/publications/tests/test_publications_ui.py::test_current_rebuild_confirmation_and_duplicate_rejection
apps/file_library/tests/test_publication.py::test_scheduled_file_publication_detail_offers_build_now
apps/file_library/tests/test_publication_reverts.py::test_previously_blocked_job_freezes_latest_source_without_review
```

Pytest used `DJANGO_SETTINGS_MODULE=config.settings.test`,
`-p no:cacheprovider -q --tb=short --durations=10`, the guarded repository scratch
directory and the isolated PostgreSQL configuration. The single pytest skip is
`tools/test_fake_ipad.py::test_secure_write_is_owner_only`
(`owner-only mode is a POSIX behavior`).
No core behavior was mocked merely to pass type checking.
The full Django suite is deliberately not run; the unchanged PostgreSQL/PostGIS
CI gate remains the release regression gate.

Native Debian 13/LXC acceptance is still required for actual systemd D-Bus
representations, credential mounts, nsenter/runuser, timer activation and Unix
ownership/ACL isolation. On Windows the deployment test explicitly skips native
Unix symlink rejection and root/service-identity filesystem checks. The other
OS/systemd boundaries are modeled in disposable storage, with real RSA/parser
validation. No deployment, real mail, live credentials, production DB writes or
Fake iPad state changes were performed.

## Changed-file inventory

- `apps/authorization/services.py`
- `apps/file_library/tests/test_publication.py`
- `apps/file_library/tests/test_publication_reverts.py`
- `apps/ingestion/tests/test_dangerous_goods_integration.py`
- `apps/ingestion/tests/test_dangerous_goods_tablet_contract.py`
- `apps/publications/document_dataset_adapters.py`
- `apps/publications/management/commands/audit_publication_retention.py`
- `apps/publications/management/commands/cleanup_orphan_artifacts.py`
- `apps/publications/models.py`
- `apps/publications/restore.py`
- `apps/publications/retention.py`
- `apps/publications/services.py`
- `apps/publications/tests/test_artifacts.py`
- `apps/publications/tests/test_fire_plan_v2_delivery.py`
- `apps/publications/tests/test_history_queries.py`
- `apps/publications/tests/test_phase4a_lifecycle.py`
- `apps/publications/tests/test_publications_ui.py`
- `apps/publications/tests/test_retention_hardening.py`
- `apps/report_transfer/pdf.py`
- `apps/report_transfer/portal.py`
- `apps/report_transfer/storage.py`
- `apps/report_transfer/tests/test_documents.py`
- `apps/report_transfer/tests/test_portal.py`
- `apps/report_transfer/transfers.py`
- `apps/tablets/tests/test_api.py`
- `deploy/lib/verify-report-transfer.sh`
- `deploy/scripts/report-transfer-credential.py`
- `deploy/tests/test_report_transfer_deployment.sh`
- `requirements/dev.txt`
- `tools/fake_ipad/client.py`
- `tools/fake_ipad/errors.py`
- `tools/fake_ipad/generations.py`
- `tools/fake_ipad/output.py`
- `tools/fake_ipad/test_generations.py`
- `tools/fake_ipad/test_output.py`
- `tools/fake_ipad/test_reports.py`
- `tools/fake_ipad/transport.py`
- `tools/test_fake_ipad.py`
- `tools/test_fake_ipad_server_contract.py`
- `docs/deployment.md`
- `docs/ci-type-deployment-compatibility.md`
