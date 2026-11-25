# Phase I targeted remediation

Reviewed baseline: `9a78c95bde02e1be1b6f338a4cb59a2daec0a40d`.
Local implementation only; no deployment or Git mutations.

## FD-SEC-001: ingress admission

The HTTPS default is now 3 MiB for ordinary forms. The actual URL configuration
and every application `request.FILES` use were checked. Exceptions are:

| Routes | Nginx body boundary | Compatibility reason |
| --- | --- | --- |
| `/api/v1/`, including adoption preview/completion | 64 KiB | Small JSON control requests; no blanket Tablet API rate limit |
| Exact `/api/v1/tablet/report-submissions` | 96 MiB | Existing encrypted report request contract |
| `/departments/<uuid>/imports/` and its six upload domain routes | 300 MiB default | Existing 256 MiB archive/file aggregate ceiling plus multipart overhead |
| Exact `/system/data/dangerous-goods/edit/` | 300 MiB default | Existing bulk import workflow |
| Legacy `/files/<uuid>/` and explorer `actions/upload/` and `actions/replace/` | 300 MiB default | Both legacy and explorer upload support batches; individual File Library files still obey the configured 25 MiB default / 100 MiB maximum |
| `/departments/<uuid>/fire-plans/` and `klgv-plans/` | 101 MiB | Existing 100 MiB PDF input bound plus multipart overhead |

All other routes retain the 3 MiB form boundary, including metadata-only import
review and document-edit actions. Application validation limits are unchanged.
The supported `MAX_INGEST_UPLOAD_BYTES` override can reach 512 MiB: rendering
scales only the bulk/batch outer allowance proportionally, with a 300 MiB floor
(600 MiB at the maximum override). This preserves existing supported overrides.
No other new ingress tuning settings were introduced.

Adoption preview/completion and check-in use a parser that reads at most 65,537
bytes and returns controlled HTTP 413 for bodies exceeding 64 KiB, even without
Nginx. Existing valid adoption and cryptographic lifecycle tests remain passing.

Login, setup (including malformed suffixes), and adoption entry routes share a
20 requests/minute per-address allowance with burst 40 and immediate processing
within that burst. Exhaustion returns HTTP 429. A 10 MiB Nginx shared zone bounds
rate-state memory. The identity is `$binary_remote_addr`; incoming forwarding
headers cannot select another bucket. Empty keys exclude other requests from
the entry bucket, preserving shared-NAT check-ins, polling and downloads.
These are initial operational values to review with representative workloads.

Existing default listeners, access/error log confidentiality, ACME challenge
routing, upstream header replacement, protected downloads, ETags and timeouts
are retained. HTTP continues its existing redirect/challenge behavior.
Large accepted requests still use Nginx request buffering. No connection ceiling
was added without measured host capacity: an arbitrary ceiling could break
supported concurrent administrative uploads. Concurrent buffering/temp-space
and worker memory sizing remain explicit native workload acceptance items.

## FD-SEC-012: indexed credential selection

`AppInstallation.credential_hash` now has a nonunique database index, introduced
by `0010_installation_credential_index`. Authentication filters on the existing
HMAC-SHA-256 digest and fetches at most two joined rows. Exactly one match is
required, followed by constant-time digest comparison. Ambiguous identity fails
closed. No public identifier, credential format, key derivation, lifecycle or
rotation contract changes. Historical credentials remain compatible; no data is
rewritten. The ordinary index migration should run through the existing owner
migration lane during deployment maintenance, never using elevated runtime grants.

Regression coverage checks the PostgreSQL index, filtered `LIMIT 2` query, one
authentication query with historical records, invalid credentials, ambiguous
digests, existing lifecycle/access matrices and adoption replay/rotation.

## FD-SEC-017: report spool capacity

Supported literal byte overrides in `/etc/fire-backend/overrides.env`:

| Setting | Default | Installer range |
| --- | --- | --- |
| `REPORT_TRANSFER_SPOOL_MAX_BYTES` | 5368709120 (5 GiB) | 1 through 1125899906842624 |
| `REPORT_TRANSFER_MIN_FREE_BYTES` | 2147483648 (2 GiB) | 0 through 1125899906842624 |

Values must be decimal integers, without unit suffixes, fractions or negatives.
Existing installations obtain defaults when overrides are absent; removing an
override restores the default. Runtime admission also rejects invalid budget or
reserve values. A zero reserve explicitly disables only the reserve check.

After authorization and exact-replay/conflict resolution, a preliminary storage
check avoids decryption when already out of capacity. After validation, the
existing transaction-scoped PostgreSQL spool advisory lock covers the final
capacity check, ciphertext write and database commit. Existing installation-first
lock ordering is retained. Different installations therefore share one admission
boundary. The check adds the incoming canonical encrypted envelope length to
the sizes of all regular spool files, including orphan ciphertext and receipt
files. Unexpected filesystem entries, inspection errors and capacity exhaustion
fail closed through the existing `report_storage_unavailable` HTTP 503 contract.
Write errors retain the existing rollback/removal behavior.

Exact accepted retries bypass capacity admission and decryption, including when
the spool is full or a newly lowered budget is below current usage. Existing
content is never evicted by admission. Existing expiry and orphan cleanup free
capacity; submission tombstones remain. Download/receipt and authorization
behavior is preserved. Tests use tiny synthetic budgets, real encrypted fixtures
and independent PostgreSQL connections for competing installations.

These controls are admission checks, not reservations against arbitrary host
disk usage outside the spool. Filesystem free space can change after inspection.
Monitor capacity and failed expiry runs; an operator-managed bounded filesystem
or OS quota remains an option. No repartitioning or host storage policy changes.

## FD-SEC-031: metadata housekeeping

The existing daily `fire-import-staging-maintenance` timer now also runs:

1. `prune_tablet_api_activity`: existing 90-day / 200-record-per-installation policy.
2. Django `clearsessions`: expired sessions only.
3. `cleanup_authentication_throttles`: counters last updated more than seven
   days ago, only when their configured failure window and any lockout have ended.

The existing boot delay, daily interval, `fire_backend` identity, credential-free
service, timeout, sandbox and installer scheduling are unchanged. Each command
is idempotent; command failure remains a failed maintenance service invocation.
No report tombstones, transfer generations, report/general audit history or
operational reference records are pruned by this change.

## Verification

Baseline Actions run `45635921317`: all five jobs completed successfully
(static checks, ACME environment, deployment security, server tests and advisory
reporting). No baseline regression requiring correction was found. Advisory
scans were not repeated locally.

Local focused results (overlapping runs, not additive unique-test counts):

- Final application/lifecycle/capacity/housekeeping run, selected portal
  download/receipt cases and installer configuration: **363 passed, 1 skipped**.
  The skip is a native Linux credential-metadata test.
- TLS/default-site/routing, installer configuration, effective systemd verifier
  and publication deployment lanes: **397 passed, 38 skipped**. Linux-only tests
  are skipped on Windows. The subsequently added spool configuration cases also
  passed in the final 363-test run. The added native ingress assertions remain
  skipped locally with their native routing fixture.
- Full repository `mypy --platform linux .`: **passed, 434 source files**.
- Changed Python Ruff lint and formatting: **passed, 16 files**.
- Changed implementation-file Bandit: **passed**.
- Tablet API validation/logging regressions: **6 passed**.
- `manage.py makemigrations tablets --check --dry-run`: **no changes detected**.
- Bash syntax for both changed shell files, body-boundary shell regression and
  existing protected-download ETag shell regression: **passed**.
- Whitespace validation: **passed**. ShellCheck is unavailable locally.

No focused test failure remains. The full application suite was not run.
Native Nginx is unavailable on this Windows host; Docker has no reachable daemon
and WSL access is denied. Native tests remain enabled for Linux CI. Their new
checks use disposable high ports, synthetic JSON and `Expect: 100-continue`
headers at large boundaries without generating huge files; these are request
admission tests, not representative upload load testing.

## Changed files

- `apps/accounts/management/commands/cleanup_authentication_throttles.py`
- `apps/accounts/tests/test_housekeeping.py`
- `apps/publications/tests/test_deployment_lanes.py`
- `apps/report_transfer/services.py`
- `apps/report_transfer/storage.py`
- `apps/report_transfer/tests/test_storage.py`
- `apps/report_transfer/tests/test_submissions.py`
- `apps/tablets/api.py`
- `apps/tablets/models.py`
- `apps/tablets/migrations/0010_installation_credential_index.py`
- `apps/tablets/tests/test_api.py`
- `config/settings/base.py`
- `deploy/lib/configuration.py`
- `deploy/lib/tls.py` (upload-boundary rendering only)
- `deploy/nginx/fire-backend.conf`
- `deploy/systemd/fire-import-staging-maintenance.service`
- `deploy/tests/test_configuration.py`
- `deploy/tests/test_nginx_body_size.sh`
- `deploy/tests/test_tls.py`
- `deploy/verify-deployment.sh`
- `docs/security-phase-i-remediation.md`

## Remaining native and privacy acceptance

- Linux CI execution of the new Nginx size/rate tests and full regressions.
- Fresh/repeated Debian 13 installation at an exact reviewed SHA, owner index
  migration, real systemd maintenance execution and observable failure reporting.
- Representative large synthetic imports and report exports, concurrent upload
  buffering, worker memory consumption and practical host sizing.
- Spool budget/free-space monitoring and actual report-expiry timer execution.
- Real backup/snapshot exclusions, swap/core-dump exposure, external telemetry
  and bearer-log boundaries, including setup/Magic-Link early failures.
- Owner-approved report metadata, audit and personnel retention policy; this
  implementation supplies no new policy for those records.
- Existing native PDF processing/containment, Nginx/Gunicorn IP attribution,
  default-site reconciliation, public HTTP-01, managed TLS staging/renewal,
  applicable recovery and firewall/SSH exposure acceptance.
- Controlled maintenance transition from pre-Phase-E PDF processing (accepted
  FD-SEC-026). Existing separately optional TLS backups remain optional; other
  applicable deployment/backup recovery acceptance remains required.

No regulatory-compliance or guaranteed denial-of-service resistance claim is made.
