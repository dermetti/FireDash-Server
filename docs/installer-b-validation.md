# Installer B local implementation report

Baseline: `46197e0029c64541cf7d7a96aa59ab6e4252bc87`.
The working tree was clean before this task. No deployment, SSH, live database
operation, production ACME request, live Netcup access, Git staging/commit/push,
branch change or pull request was performed. No application or migration files
were changed. Generated key material was confined to disposable test fixtures.

## Modes and transition

The supported modes are `manual`, `letsencrypt-http01` and
`letsencrypt-dns01`. New public installations default to HTTP-01; existing
installations without a mode remain manual/external. DNS-Netcup supports fresh
issuance and explicit adoption with `--tls-adopt-dns01`. Existing manual-to-DNS
adoption is permitted only with that explicit flag and successful lineage
validation. Other mode changes stop for review.

Healthy DNS adoption makes no issuance/renewal request and preserves certificate
paths, private keys, account state, credential contents and renewal INI bytes.
DNS-01 makes no DNS-to-interface or inbound HTTP/HTTPS preflight. It works with
CGNAT and retains the existing authenticator. Actual scheduled renewal uses
Certbot's stored lineage configuration; normal Certbot bookkeeping during a
renewal remains Certbot's responsibility, without FireDash INI patching.

## Files

New files:

- `deploy/lib/tls.py`: root-only lifecycle, DNS adoption, scoped renewal,
  transactional activation/recovery, staging dry-run and verification.
- `deploy/systemd/fire-tls-renew.service` and `fire-tls-renew.timer`.
- `deploy/tests/test_tls.py`: synthetic certificate, state/failure, credential,
  DNS/CGNAT, concurrency, no-op and native Nginx fixtures.
- `docs/installer-b-tls.md` and this report.

Modified files:

- `deploy/install-local.sh`: validated mode inputs, explicit DNS adoption,
  premaintenance TLS phase, persisted settings and conditional timer activation.
- `deploy/lib/configuration.py`: literal TLS configuration allowlist/validation.
- `deploy/lib/nginx.sh`: transactional Python installation boundary; correct
  OpenSSL hostname-result handling in its retained validation helper.
- `deploy/nginx/fire-backend.conf`: HTTP challenge location and location-level
  ordinary redirect, retaining protected locations and log suppression.
- `deploy/verify-deployment.sh`: certificate lifecycle verification in addition
  to existing application, security and HTTPS readiness checks.
- `deploy/tests/test_configuration.py`: preserve existing orchestrator/security
  tests with the new TLS boundary; add mode-change and premaintenance-failure checks.
- `.github/workflows/ci.yml`: install native Nginx test binary and include TLS
  tests in deployment-security validation. No CI workflow was dispatched.
- `docs/deployment.md`, `docs/configuration.md`, `docs/operations.md`,
  `docs/acceptance.md`: supported workflows, settings, operations and pending gates.

The existing stage-0 bootstrap, exact-SHA release logic, bootstrap package list,
systemd installation helper, application identities, PostgreSQL ownership,
credential selectors and application sandbox units remain unchanged.

## Issuance, renewal and failure handling

HTTP-01 verifies a lowercase FQDN and every locally resolved A/AAAA record against
public local interfaces, then installs only a controlled HTTP bootstrap site.
The challenge bypasses HTTPS redirection; ordinary bootstrap requests return 503.
Local per-address challenge probes precede Certbot, but do not claim to prove
Internet reachability. Certbot webroot issuance runs before application maintenance.
The final HTTPS site is activated only with a matching, hostname/date-valid pair.
Unrelated Nginx sites, including the default site, are retained.

Fresh managed issuance uses an isolated config/work/log namespace. Fresh DNS-01
requires an existing reviewed Certbot/DNS-Netcup environment and private credential
file; it uses a 900-second propagation wait without HTTP probes. Adopted DNS-01
uses the exact named host lineage, its existing credential path and original
certificate paths. Credential files must remain root-owned 0600 with protected
parents; their contents are never loaded into Django configuration or diagnostics.

The root oneshot service is filesystem-hardened, has no application environment
or application credentials, and renews only `--cert-name <selected-lineage>`.
Its timer runs twice daily, with randomized delay and persistent retry scheduling.
It takes the same exclusive lock as the installer. Certbot namespace locks provide
additional coordination. DNS adoption rejects competing active/enabled host
Certbot units and ACME cron/timer ownership; it does not disable unrelated jobs.
Disabled/static units and the dormant systemd-guarded Debian cron entry are allowed.

The deploy hook validates certificate/key/hostname/dates and Nginx before reload.
Fresh lineages expose verified copies via an atomic pointer with rollback.
Adopted lineages retain a private recovery journal of standard Certbot archive
link targets and restore those targets on failed renewal/activation. Archive
certificates, private keys and account state are not deleted. Repeated renewals
that are not due do not reload Nginx. The dry-run entrypoint uses staging validation
without deploy hooks or application changes. Renewal never invokes Gunicorn,
Django, migrations or database tools.

Expired managed certificates can renew before the final validity gate; expired
manual certificates fail with external-owner recovery instructions. ACME failure
precedes application quiesce. Failed Nginx configuration/reload restores the
previous site or certificate pointer. Interrupted first issuance can retry the
controlled bootstrap; incomplete Certbot state stops for private operator review
rather than deleting or regenerating keys to force convergence. Existing
interrupted application upgrades retain the original maintenance recovery semantics.

## Local validation

Windows/Git Bash, using `config.settings.test` and `-p no:cacheprovider`:

| Check | Result |
| --- | --- |
| `pytest deploy/tests/test_configuration.py deploy/tests/test_tls.py` | 195 passed, 19 skipped |
| Existing Bash deployment regressions | 18 passed, 1 skipped |
| Bash syntax, all deployment/library/test `.sh` files | 36 passed |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed, 446 Python files |
| Relevant mypy with Linux target | Passed, 4 changed Python files |
| Bandit on TLS/configuration helpers | Passed; intentional argument-list subprocess use explicitly annotated |
| Configured application Bandit scan | Passed; existing annotation warnings only |
| Django `check --settings=config.settings.test` | Passed |
| `git diff --check` | Passed; Windows line-ending notices only |

The focused pytest command includes `--ds=config.settings.test
-p no:cacheprovider -q --tb=short`. Its 19 skips require native Linux symlinks,
permissions/locking or Nginx and are explicitly marked. They cover full fresh
HTTP bootstrap/activation retries, certificate pointer rollback, DNS symlink
renewal/rollback/interruption, fresh DNS issuance, shared-lock exclusion,
actual Nginx HTTP routing and site rollback. They were not executed on Windows.
The Bash skip is the existing root-only Linux signing-key integration test.

Three initial Bash failures came from the WindowsApps `python3` launcher. Those
three passed with a disposable shim selecting the existing repository virtualenv;
no production script or test assertion was weakened. Initial Python failures
identified Windows path-conversion fixture issues and OpenSSL versions returning
zero for hostname mismatch; both were corrected before the passing result.

## Required evidence and limitations

Installer B acceptance is pending. No real ACME integration, real Netcup API
validation, native systemd execution or production renewal is claimed from mocks.
ShellCheck was unavailable locally and is not configured in the existing CI;
Bash syntax and existing regression tests were run instead.

After review and separately authorized commit/push, the full GitHub CI suite is
mandatory. The native fixtures must pass on Linux, and an isolated Debian 13
systemd environment must demonstrate actual HTTP-01 and DNS-Netcup staging
issuance/renewal, scheduler/sandbox behavior, failure/retry and application-state
preservation with disposable domains/accounts/credentials. Test the exact
installed Certbot/plugin versions and live/archive layout. Only after separate
deployment authorization can the existing Proxmox DNS-01 installation be validated;
its live certificate and credentials were not accessed here.

Remaining operational constraints are documented in the TLS runbook: direct
public A/AAAA interfaces for the default HTTP-01 preflight, explicit ownership
reconciliation on hosts with other ACME schedulers, private ACME/credential backup,
and external monitoring of expiry warnings and failed units. No alert-delivery
integration or automatic external-to-HTTP conversion is supplied.
