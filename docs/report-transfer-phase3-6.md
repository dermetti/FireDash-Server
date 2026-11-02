# Phase 3.6 — standard deployment integration

Implemented locally; no deployment, SSH, real email, live database, production
credentials, Fake iPad identity or dataset modifications were performed.

The standard installer now provisions the dedicated RSA credential and private
spool, installs/enables expiry, and rejects a disabled or insecure Report Transfer
configuration. ReportDocument, JWE, submission APIs, recipient policy, Magic Links,
retention and legacy PDF-Mail are unchanged. No application migrations are needed.

## Changed-file inventory

| Files | Change |
| --- | --- |
| `deploy/install-local.sh` | Quiesce before secrets; missing-key checks cannot race report uploads. |
| `deploy/bootstrap-lxc.sh` | Ensure `util-linux`/`nsenter` availability; revalidate spool after tmpfiles. |
| `deploy/lib/secrets.sh` | Preserve active version, generate only proven-safe initial RSA keyring, validate/preserve existing keys, initialized marker. |
| `deploy/lib/report-transfer.sh` (new) | Small shared private spool create/validate helper, no content operations. |
| `deploy/scripts/report-transfer-credential.py` (new) | RSA-3072 PKCS#8 generation, real runtime-parser validation and running-web credential namespace probe. No secret output. |
| `deploy/scripts/create-service-users.sh` | Ensure spool after creating the service identity, before tmpfiles/application activation. |
| `deploy/lib/systemd.sh` | Retire exact old example drop-in; fail on mixed conflicts; expiry installation via existing glob, quiesce/restart participation and explicit activation failure propagation. |
| `deploy/systemd/fire-backend.service` | Dedicated report credential and spool write path, existing hardening/publication isolation retained. |
| `deploy/systemd/fire-backend.tmpfiles.conf` | Private report spool definition. |
| `deploy/systemd/fire-report-expiry.service`, `fire-report-expiry.timer` (new) | Canonical production cleanup, no RSA private key, narrow write boundary, five-minute schedule. |
| `deploy/lib/verify-report-transfer.sh` (new), `deploy/verify-deployment.sh` | Effective unit/access/credential/origin/privacy checks, actual running-web configuration load; failures enter existing verifier accumulator. |
| `deploy/scripts/fire-backup` | Explain existing explicit-source boundary: credential keyring included, temporary spool/receipts excluded. |
| `deploy/tests/test_report_transfer_deployment.sh` (new) | Disposable real RSA/parser/file tests and mocked OS/systemd/verifier fault injection. |
| `deploy/tests/test_report_transfer_installer.sh` (new) | Actual installer phase block with stubs; both real secret initialization branches include Report Transfer. |
| `deploy/tests/test_systemd_quiesce.sh` | Expiry stop/order/rerun/activation failures plus existing timer assertions. |
| `tools/test_report_transfer_deployment.py` (new) | Runtime configuration acceptance/rejection, namespace invocation, privilege drop and no secret arguments. |
| `deploy/examples/report-transfer/*.example` (four deleted), `README.md` (new) | Remove duplicate authoritative definitions; point to canonical units. |
| `docs/deployment.md`, `docs/report-transfer-phase3-3.md`, this document | Operations, upgrade/restore boundaries, validation and operator-controlled redeployment. |

Inspected unchanged installer entrypoints, release/initialization helpers, settings,
crypto contract, backup sources, Phase 3.4 tooling and deployment test conventions
are reused. No separate installer or enablement flag was added.

## Preservation and fail-closed behavior

Existing valid keyring bytes and every key version are preserved. Initial creation
uses a `0600` staging file inside the root-only credential directory and atomic
installation. Invalid JSON, duplicate JSON keys, invalid PEM, non-RSA/weak RSA,
missing active version and unsafe ownership/mode do not authorize replacement.
Missing keys with database records, spool contents, prior initialization evidence
or uncertain database state require restoring credentials. Pre-migration table
absence is checked separately from a broken previously migrated database.

The exact old example drop-in is retired; unrelated admin configuration survives.
Mixed report overrides and custom storage/direct-read credential environment paths
stop for explicit reconciliation. This avoids abandoning existing encrypted data.
There is no automatic rotation, spool deletion or receipt removal.

The web configuration probe copies the running process environment in memory,
enters its mount namespace as root, then invokes the real parser/configuration as
`fire_backend`. Database passwords stay in the environment, never arguments or
output. It verifies production settings, the credential-directory path, current
active version, HTTPS origin, spool access, enabled v1 JWE parameters, public-key
derivation and JPEG support. Cleanup receives the runtime database environment
without the report RSA credential. Verification performs no report submission or
mail send.

## Executed local validation

Windows/Git Bash, disposable directories and generated non-production keys; Django
uses the isolated PostgreSQL/PostGIS test settings. Never run the installer itself
as a test. Bash helper results below are script results, not pytest case counts.

```powershell
$bash = 'C:/Program Files/Git/bin/bash.exe'
& $bash -c 'FIREDASH_TEST_PYTHON=$PWD/.venv/Scripts/python.exe bash deploy/tests/test_report_transfer_deployment.sh'
& $bash deploy/tests/test_report_transfer_installer.sh
& $bash deploy/tests/test_systemd_quiesce.sh

$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider apps/report_transfer/tests/test_submissions.py apps/report_transfer/tests/test_authorization_audit.py -q --tb=short
.venv/Scripts/python.exe -m pytest --ds=config.settings.test -p no:cacheprovider tools/test_report_transfer_deployment.py -q --tb=short
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/ruff.exe check deploy/scripts/report-transfer-credential.py tools/test_report_transfer_deployment.py
.venv/Scripts/ruff.exe format --check deploy/scripts/report-transfer-credential.py tools/test_report_transfer_deployment.py
```

Results:

- Report submission and authorization/audit: **92 passed in 67.97 seconds**.
  Includes JWE/configuration/rotation, durable replay, invalid scope/recipient,
  concurrency, cleanup/retention and audit coverage.
- New namespace/configuration probe: **8 passed in 0.97 seconds**. Initial test
  run had one Windows fixture-path mismatch (7 passed/1 failed); fixture corrected
  to express the Linux canonical path, then all eight passed.
- New deployment helper: passed real RSA generation/runtime parsing, configured
  active version, byte-preserving reruns/historical versions, malformed/weak keys,
  missing-credential restoration guards, spool-content/receipt preservation,
  override reconciliation and mandatory verifier fault injections. Unix symlink,
  ownership and real identity access enforcement require Linux; those OS
  boundaries are modeled here and explicitly reported as a platform skip. A
  gated native branch exercises real ownership/modes, backend writes, denied
  worker/Nginx access and receipt preservation when root-capable isolated Linux
  already provides the service identities; it creates no host users.
- Installer phase/secret-entrypoint and systemd/quiesce tests: passed. The first
  new installer-test invocation exposed an empty stub rejection value; the stub
  was corrected, and the actual phase assertions passed. Expected injected stop
  failures print errors as part of successful negative tests.
- Existing deployment scripts passed: `test_application_secret_keyring.sh`,
  `test_outbound_mail_proxy_env.sh`, `test_deprecated_env.sh`,
  `test_release_activation.sh`, `test_verify_django_checks.sh`,
  `test_hba_verify.sh`, `test_nginx_etag.sh`, `test_state.sh`,
  `test_release_venv.sh`, `test_converge.sh`, `test_postgres_bootstrap.sh`,
  `test_postgres_utf8.sh`, `test_nginx_body_size.sh`.
- Platform limitations: `test_release_activation.sh` skips its native Unix
  symlink switch; `test_state.sh` skips its symlink case.
  `test_rotate_publication_signing_key.sh` was attempted and reports SKIP with
  exit 1 because it needs isolated root-capable Linux. It was not counted as a
  passing integration test. WSL distribution enumeration returned access denied;
  native systemd/credential mounts, timer execution and live verifier execution
  were not attempted. ShellCheck is unavailable locally.
- `bash -n` passed for all changed shell files; Django system checks reported no
  issues, migration drift reported no changes; Ruff lint/format and whitespace
  diff checks passed. No full Django suite was run.

The existing secret and verifier scripts were invoked with the same Git Bash/
`FIREDASH_TEST_PYTHON` wrapper shown above; none contacted a server. No browser UI
tests were added.

## Compatibility risks and remaining target acceptance

Run the isolated Bash tests on root-capable Debian 13 before release acceptance,
then let the operator-controlled standard installer perform its final verifier.
Real systemd effective-property formatting, mount-namespace entry capabilities in
the chosen LXC, identity/ACL enforcement and the timer's actual execution remain
Linux acceptance items. The new verifier fails rather than silently bypassing a
namespace capability limitation. `util-linux` is explicitly provisioned.

Invalid credentials, custom report paths or mixed drop-ins intentionally block
convergence. Maintenance now starts earlier, before the secret check; interruption
may leave services stopped. Restore/reconcile and rerun the same exact-SHA
installer. Full-volume/hypervisor snapshots cannot be excluded by this checkout;
their operators must exclude the temporary spool and receipts while retaining
protected credential backups. Existing SMTP/provider configuration remains an
application prerequisite and is not tested with real email.

The exact operator-controlled redeployment and read-only troubleshooting commands
are in [deployment.md](deployment.md#operator-controlled-redeployment-after-review).
No deployment has been performed and no release success is claimed here.
