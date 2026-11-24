# Installer B.2 local implementation and validation

Reviewed HEAD: `27d127fc01cf3d70a362231a40aca53cb232c54f`; the initial working
tree was clean. Changes are local only. No deployment, SSH, production ACME,
live DNS credentials, database operations, application migrations, key rotation,
Git staging/commit/push or destructive Restic operation occurred.

## Recovery durability

The old `finally` blocks removed the recovery journal even when restoring a
symlink failed. They also treated an attempted restoration as sufficient cleanup,
without establishing a complete pair and consistent Nginx activation.

Version-2 journals are now exclusively published before mutation and fsynced,
with `pending`/`rollback` states, lineage identity and original archive links.
All targets are checked before the first replacement. Each link update is atomic
and directory-synced. Recovery validates the complete restored generation and
certificate/key/hostname/validity, waits for any outstanding reload, then requests
a distinct reload of restored files. Only verified activation permits durable
journal removal. Failed or interrupted restoration/reload retains the journal.
New renewal/dry-run resolves existing recovery first; unresolved information is
never overwritten. Explicit locked `tls.py recover` is idempotent. Missing archives,
unsafe/old journal formats and expired rollback material fail closed for private
operator review. Certbot accounts, archives, keys and renewal INIs are not rewritten.

## Encrypted backup and isolated restore

The dedicated root-only `tls_backup.py` helper and service/timer reuse Restic
encryption with a **separate restricted HTTPS REST repository**. This avoids
changing application backup authorization, sources, history or retention. There
are no initialization, forget, prune or live-restore operations. `/var/spool` is
inaccessible in the service sandbox and excluded from input selection.

The snapshot selects FireDash install/TLS metadata and verified generations,
the single configured live/archive/renewal lineage, its referenced account,
protected DNS credentials, dependency lock and plugin provenance. It excludes
unrelated certificates/accounts, work/log directories, application credentials,
database/storage data, temporary spools/receipts and repository access/encryption
secrets. The latter must remain in an independent operator recovery store.

The daily randomized timer and renewal success/failure triggers take the shared
installer lock. Confirmation requires Restic's full snapshot ID. Verification
checks exact inventory, repository binding, a 48-hour receipt, installed units
and an enabled/active timer. Failed backups are observable and cannot advance
the last-success receipt. There is no automatic TLS pruning.

Restoration accepts an explicit TLS-tagged snapshot ID and an empty named child
of `/var/lib/fire-backend-tls-restore`. It verifies hashes, ownership, modes,
relative symlinks, account/renewal metadata, protected DNS credentials, certificate
identity/key/hostname/validity and approved installed dependency versions. It
never executes restored programs, activates files, enables timers or starts
services. An unresolved journal requires explicit isolated recovery review.
See [operator procedure](tls-recovery.md) for exact provisioning/restore steps.

## Local verification

- Focused pytest: **277 passed, 26 skipped, 0 failed** in 63.97 seconds:
  `python -m pytest --ds=config.settings.test -p no:cacheprovider deploy/tests/test_tls.py deploy/tests/test_configuration.py -q --tb=short`.
  Skips are explicit native Linux gates. New coverage includes partial-link
  failure, interrupted reload, malformed journals, missing archives, retry/no-op,
  unresolved-journal renewal refusal, encrypted input selection, secret isolation,
  failed uploads, freshness/repository binding, isolated restore failures and
  existing Netcup/fresh multi compatibility. Existing HTTP/security tests remain.
- Bash syntax: **40 passed** (deployment `.sh` files plus backup/restore scripts).
- Bash regressions: **18 passed, 1 skipped, 0 failed**. The root-only signing-key
  integration script returned its documented exit 77 on Windows.
- Ruff repository check passed; formatting check: **448 files** unchanged.
- Relevant mypy: **5 files passed**, using Linux platform semantics.
- Bandit: TLS manager, DNS adapter and backup helper passed.
- Django test-settings system check: **0 issues**. No database query/migration run.
- CI YAML parsing and Git whitespace checks passed.
- Final recovery/backup subset after temporary-path hardening: **24 passed,
  7 skipped, 106 deselected**, 0 failed in 5.55 seconds. Ruff, mypy, Bandit
  and whitespace checks were repeated successfully.

Initial test-fixture failures involved Windows path spelling and were corrected;
the final run above has no failures. No full local Python suite was run.

## Pending acceptance and remaining risks

Native Linux could not run here: WSL enumeration returns access denied and no
Docker daemon is reachable. ShellCheck is unavailable. The 26 native skips,
including durable symlinks and actual disposable Restic encryption/restore, remain
pending. CI now installs Restic alongside Nginx for the native round-trip test;
the new CI run requires separately reviewed/authorized commit and push.

Ready for **disposable-environment acceptance**, not live deployment. Demonstrate
real root metadata, Debian 13/systemd sandbox/timers, interruption during reload,
lock contention, encrypted REST upload and isolated restore, plus actual staging
renewal/failure/retry with disposable DNS credentials. No real restore, staging
ACME or production Netcup validation is claimed by the local results.

Provision and independently verify the dedicated repository/identity and off-host
recovery secrets before acceptance. Equal repository settings are rejected, but
different URLs aliasing the same repository must be excluded operationally.
Local success receipts do not prove continuing remote availability; monitor and
perform periodic restore drills. Old-format journals and expired rollback targets
need explicit review; no unsafe automatic conversion is provided. TLS retention
and capacity require a separately authorized policy. Provider support and privileged
dependency versions were not expanded.

## Changed files

- Recovery: `deploy/lib/tls.py`.
- New backup boundary: `deploy/lib/tls_backup.py`,
  `deploy/systemd/fire-tls-backup.service`, `deploy/systemd/fire-tls-backup.timer`.
- Wiring: `deploy/install-local.sh`, `deploy/lib/systemd.sh`,
  `deploy/systemd/fire-tls-renew.service`, `deploy/verify-deployment.sh`.
- Tests/CI: `deploy/tests/test_tls.py`, `deploy/tests/test_configuration.py`,
  `.github/workflows/ci.yml`.
- Documentation: this report, `docs/installer-b-tls.md`, `docs/tls-recovery.md`,
  `docs/operations.md`, `docs/acceptance.md`.

`deploy/scripts/fire-backup` and `deploy/lib/dns_auth.py` remain unchanged.

---

# Historical Installer B.1 local implementation and validation

Retained as phase evidence. Current recovery and backup behavior is superseded
by B.2 above; earlier backup implementation blockers describe the B.1 state.

Reviewed and inspected baseline: `08f4c21ec3fcf444e67605bab10ddd2ec6f264b9`.
HEAD matched the review and the initial working tree was clean. This report
supersedes the current-behavior portions of the retained Installer B report below.
No deployment, SSH, live database operations, production ACME issuance, live DNS
credentials, application/migration/cryptography changes or Git mutations occurred.
Key generation was confined to synthetic disposable certificate fixtures.

## Architecture and configuration

The existing `tls.py` lifecycle, three certificate modes, HTTP routing, systemd
service/timer, shared installer lock, certificate validation, scoped renewal and
rollback remain. New `dns_auth.py` contains a small explicit adapter registry:

- `certbot-multi`: fresh DNS-01 using one pinned plugin/bridge for multiple
  providers. Authenticator is `dns-multi`; provider and secrets stay in its
  protected INI. Fresh HTTP-01 remains the public VPS default; manual is unchanged.
- `certbot-existing`: explicit adoption of a named compatible Certbot DNS lineage.
  The saved authenticator must match the operator-approved name, be installed,
  and use protected file credentials. Healthy adoption never issues/renews,
  edits the INI, changes accounts/keys/paths/credential contents or changes the
  running application during TLS preparation. Tests cover Netcup, Cloudflare
  native and dns-multi adoption.
- No fresh `certbot-native` backend is added. Native plugins remain usable through
  generic adoption. No arbitrary plugin imports, shell programs, provider API
  implementations or second ACME engine are introduced.

New root-only install.conf fields (also validated, persisted and verified):
`FIREDASH_TLS_DNS_BACKEND`, `FIREDASH_TLS_DNS_PROVIDER`,
`FIREDASH_TLS_DNS_AUTHENTICATOR`, `FIREDASH_TLS_DNS_PLUGIN_APPROVED`,
`FIREDASH_TLS_DNS_PROPAGATION_SECONDS`. Existing credential path, executable,
lineage and adoption settings remain. CLI names and interactive/noninteractive
examples are in [the operator workflow](installer-b-tls.md).

Missing backend on an already persisted Installer B lineage is an internal
Netcup **renewal-only** compatibility path, not a new selectable backend.
Pre-B missing mode remains manual. Explicit backend/authenticator/provider,
executable, lineage, adoption and credential-binding changes on established DNS
installations fail before quiesce for transition review. The old Netcup credential
flag remains a path-only alias. The current Proxmox Certbot 5.7.0 environment,
`dns-netcup`, live paths, account, private key and credential INI are retained;
no inbound port or public-IP check is performed for either DNS backend.

## Dependencies, credentials and privileged code

Candidate versions: Certbot/acme **5.7.0**, certbot-dns-multi **5.6.0**, embedded
lego **v5.6.0**. Upstream metadata, tagged plugin arguments and tagged bridge
manifest were inspected. A Linux CPython 3.13 dependency resolution produced
`deploy/acme/requirements.lock`: all 18 resolved packages pinned with SHA256
hashes. Resolution establishes metadata compatibility, not native runtime proof.

`deploy/acme/install-environment.sh` is an explicit operator-only Debian 13 amd64
offline wheelhouse installation. It refuses existing environments, takes the
shared lock, uses hash enforcement and binary wheels only, runs pip check and
activates a separate protected `/opt` environment after success. Neither an
application upgrade nor renewal invokes it. No system Python or Django virtualenv
is modified. Dependency changes require provenance/advisory review, a new lock,
version checks and native/staging acceptance. Existing native plugin environments
are approved explicitly, not silently upgraded.

The bounded registry contains cloudflare/netcup/hetzner/ovh/digitalocean/desec/
gandiv5/porkbun. Exact plugin availability and pinned multi/Certbot metadata are
checked. A provider being included in this bridge/registry does not mean FireDash
end-to-end acceptance passed. Unreviewed providers, external execution/manual/
webhook mechanisms, global environment controls, duplicate INI keys and `_FILE`
indirection fail closed. Provider API details remain upstream.

Credential files must be regular root:root 0600, with protected root-owned
parents; credential contents never enter installer/app configuration, argument
values, output or exception diagnostics. ACME programs/renewal INIs must also
have protected root-controlled metadata. Privileged child commands receive a
minimal fixed environment, excluding ambient provider secrets, Python/loader
injection and shell startup overrides. Approval covers the selected privileged
Certbot environment and its installed plugins; it is an operator provenance
attestation, not an executable sandbox or automatic source-code audit.

## Renewal, verification and recovery

The unchanged service/timer runs only `renew --cert-name <selected-lineage>`.
Saved authentication is retained; no auth override is passed on renewal.
Plugin availability, credentials, lineage server/authenticator, hooks and scheduler
conflicts are checked before renewal. Certificate/key/hostname/dates and Nginx
validation precede reload; no-op renewals do not reload. No Django/Gunicorn
restart, application calls or database operations are added. Existing atomic
verified copies, adopted archive-link recovery journal and timeout/interruption
rollback remain. Certbot account/archive state is retained. Failed activation
restores prior deploy state; ACME failures happen before installer maintenance.

Sanitized command errors classify provider authentication, propagation/validation
and general ACME failure when evidence permits. Missing plugin, renewal mismatch
and certificate validation errors have separate fixed diagnostics; ambiguous
provider errors remain generic. Raw plugin diagnostics are never replayed.
The verifier now includes the adapter helper in installed-file parity checks.

## Backup/recovery gap ? beta blocker

The existing encrypted backup script was inspected and left unchanged. Its sources
do **not** cover all TLS/ACME state; its credential source alone is insufficient.
A narrowly scoped encrypted Restic manifest and isolated restore procedure are
documented in the TLS workflow: installer config, managed copies/recovery metadata,
private ACME namespace or selected adopted renewal/live/archive/account, DNS
credentials and pinned provenance. No report spool, unrelated retention change,
backup pruning or production restore is introduced.

**Automatic TLS snapshot integration is not implemented. A separately reviewed
root-only encrypted snapshot schedule and successful isolated restore drill are
beta-blocking requirements. Recovery is not claimed complete.**

## Changed files

New: `deploy/lib/dns_auth.py`, `deploy/acme/requirements.in`,
`deploy/acme/requirements.lock`, `deploy/acme/install-environment.sh`.

Modified: `deploy/lib/tls.py`, `deploy/lib/configuration.py`,
`deploy/install-local.sh`, `deploy/tests/test_tls.py`,
`deploy/tests/test_configuration.py`, `.github/workflows/ci.yml`,
`docs/installer-b-tls.md`, `docs/installer-b-validation.md`,
`docs/configuration.md`, `docs/deployment.md`, `docs/operations.md`,
`docs/acceptance.md`.

Inspected and retained: nginx library/template, exact-SHA entrypoint, TLS renewal
units, deployment verifier wrapper and protected backup workflow. New helper
installation and verification extend the existing paths. No existing security
regression cases were removed; the old fresh native-Netcup fixture now exercises
fresh dns-multi for Netcup/Cloudflare, while native-Netcup adoption stays covered.

## Local evidence and limitations

Focused command: `.venv/Scripts/python.exe -m pytest --ds=config.settings.test
-p no:cacheprovider deploy/tests/test_tls.py deploy/tests/test_configuration.py
-q --tb=short`.

Final complete result: **252 passed, 20 skipped, 0 failed**, 56.53 seconds.
The 20 skips are native Linux symlink, permissions, locking and actual Nginx
cases, including two fresh multi providers. The final run includes child-
environment isolation, exact plugin-heading detection and sanitized failure
classification (generic HTTP/filesystem denial is not labeled DNS authentication).
Earlier intermediate fixture failures (new managed helper/service boundaries
on Windows) were corrected by mapping those host boundaries into the private
installer harness. No security assertions were removed to pass tests.

Additional local results:

- Bash syntax: **38 passed, 0 failed**, plus the parsed new CI container command.
- Existing Bash regressions: **18 passed, 1 skipped, 0 outstanding failures**.
  The skip is root-only native Linux signing-key integration (exit 77). The Report
  Transfer deployment regression exceeded the initial 120-second runner budget,
  then passed unchanged with a 600-second limit; the remaining scripts also passed.
  A disposable python3 shim selects the existing test venv on Windows.
- Ruff check and format check: passed; **447** Python files already formatted.
- Relevant mypy: passed, **5** source files.
- Bandit TLS/configuration helpers: passed. Configured application Bandit scan:
  passed with existing nosec/comment warnings.
- Django check with test settings: passed, no issues.
- CI YAML parsing and new Debian container command Bash syntax: passed.
- Git diff whitespace check: passed; Git index/refs remain untouched.

Tagged upstream source confirmed the credential/propagation interface and all
eight registered provider names in the bundled lego factory; the provider source
files do not directly import os/exec. This is bounded source inspection, not a
complete audit of third-party code and transitive SDKs. Dependency resolution used
only public metadata/artifacts and did not install privileged code.

Native Linux execution is unavailable: WSL enumeration returns access denied;
Docker has no reachable daemon. ShellCheck is unavailable. Real ACME, DNS API,
staging/systemd and full GitHub CI were not executed. The added Debian 13 CI job
installs and checks the pinned environment without accounts/credentials/issuance;
it remains unrun until separately reviewed commit/push. Existing deployment-
security checks remain in CI.

Pending integration: disposable Debian 13 native tests and sandbox verification;
actual Let's Encrypt staging issuance/renewal with disposable DNS credentials;
systemd unattended renewal, failure/retry and concurrency; native existing Netcup
adoption validation; second real provider when credentials are available;
application/key preservation evidence; full GitHub CI; encrypted TLS backup and
restore evidence. Provider credentials were not supplied or requested here.
No mocked success is presented as external ACME acceptance.

---

# Historical Installer B local report (superseded by B.1 above)

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
