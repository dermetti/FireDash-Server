# Deployment

This is the installation authority for FireDash Server. Runtime procedures are
in [operations.md](operations.md); setting names are in
[configuration.md](configuration.md).

## Supported target

Deploy supported releases to a Debian LXC host with PostgreSQL 17/PostGIS,
Nginx, Gunicorn, and systemd. Releases are immutable under
`/srv/firedash/releases/<sha>` and `/srv/firedash/current` points at the active
release. Do not edit a deployed release in place.

The convergent installer creates accounts, directories, PostgreSQL roles,
environment files, credentials, Nginx configuration, systemd units, static
assets, and migrations. Re-running it must repair the intended state without
depending on a prior partial run. Run the deployment verifier after every
installation or upgrade.

TLS certificates must be supplied through the supported installer configuration.
Automatic Let's Encrypt issuance and verbose/quiet installer modes are planned
improvements, not current installer capabilities.

## PostgreSQL and PostGIS

Provision PostGIS before migrations. Use separate roles:

- **database owner** applies migrations and owns schema changes;
- **application runtime** has only the application data permissions it needs;
- **backup role** can make verified backups but cannot modify audit records.

Use local HBA rules and protected credential files appropriate to those roles.
Do not give Gunicorn ownership credentials, disable triggers, or broaden HBA
authentication to troubleshoot an application issue. Database constraints and
audit immutability are deployment requirements, not optional application
features.

## Web and filesystem layout

Nginx terminates TLS and proxies Gunicorn over its local Unix socket. Install
the intended certificate and validate Nginx before reloading it. Nginx serves
protected publication artifacts through an internal alias rooted at
`/var/lib/fire-backend/publications/`; Django authorises access and uses
`X-Accel-Redirect`, rather than streaming artifact bytes.

Keep ownership and writable paths narrow. Publication artifacts, accepted
fire plans, quarantine, sanitizer output, static assets, and backups each
have separate controlled locations. Do not make broad paths writable or use
ad-hoc `chmod`/`chown` changes as a deployment shortcut.

## Services and credentials

`fire_backend` runs Gunicorn. It has no publication KEK or Ed25519 private
signing key. `fire_publication` owns the hardened publication workers:

- `fire-publication-delivery.service` is persistent and runs
  `process_publication_jobs --delivery --forever --poll-seconds 2` for grants
  and signed manifests.
- `fire-publication-build.service` runs build-only work. Its timer runs daily
  at 00:05; `fire-publication-build.socket` accepts an advisory local wake
  from the web process for eligible manual work.
- `fire-publication-maintenance.service` and timer perform retention and
  cleanup without the KEK or private signing key. Maintenance protects the
  current publication and two usable rollback predecessors, obsoletes older
  successful versions, and purges only aged source snapshots from failed or
  cancelled attempts.
- `fire-import-staging-maintenance.service` runs as `fire_backend` daily to
  expire private import staging. Its only writable filesystem exception is
  `/var/lib/fire-backend/import-staging`.

The build socket is `/run/fire-backend/publication-build.sock`, group-owned
for the web service with mode `0660`. Connecting carries no job data and gives
the web process no systemd, sudo, D-Bus, or arbitrary service-start privilege.
The database remains the queue. Credential files for KEK/signing material are
loaded only into the delivery/build services with `LoadCredential`. The
root-owned `publication-signing-public-key-ring.json` is separately delivered
read-only to web and publication services; it retains historical public
Ed25519 keys by version. Use the root-only
`/srv/firedash/current/deploy/rotate-publication-signing-key prepare --version N` followed by its
verification and `activate --version N` workflow rather than editing active
credentials by hand. The helper preserves the old public entry, validates the
staged private/public pair against the ring, and updates only the active worker
credentials and `PUBLICATION_SIGNING_KEY_VERSION`. A normal exact-SHA installer
rerun validates and preserves the resulting ring. See the full runbook in
[operations.md](operations.md#publication-signing-key-rotation). Never remove
an old public version while retained manifests or artifacts may refer to it.

The installer retires the obsolete generic publication-worker timer, starts
the persistent delivery service, and enables the build socket, nightly build
timer, and maintenance timer. It also installs the PDF sanitizer broker and
per-job sandbox units. Preserve the unit hardening directives rather than
copying commands into a less restricted service.

Report Transfer accepts encrypted JSON/JPEG packages and generates portal PDFs.
Direct tablet PDF-mail admission is retired. The separate reference-data PDF
sanitizer remains qpdf-based. See [Report Transfer](report-transfer.md).

## Optional outbound-mail HTTPS egress proxy

Report Transfer infrastructure is mandatory and automatically provisioned; it
does not depend on choosing an outbound proxy. See the operational details below.

Direct HTTPS egress is the default. Installations that require a fixed or
NAT-controlled egress path may configure an HTTP(S) proxy with the persistent
deployment option:

```sh
sudo deploy/install.sh --outbound-mail-https-proxy=http://proxy.example:3128
```

The installer stores this deployment-owned value in
`/etc/fire-backend/install.conf` and renders it into
`/etc/fire-backend/fire-backend.env`, which is loaded by `fire-backend.service`.
It is retained on exact-SHA upgrades. Pass an empty value to clear it. The
value must be an `http://` or `https://` URL with a hostname; invalid values
fail before activation. This is not an administrator or database setting.

## Reference-data sandbox

The root-owned PDF sanitizer broker remains outside the web process. Its Unix
socket is accessible to the web service only, accepts a canonical job UUID,
and launches a per-job `fire_pdf_sanitizer` sandbox with private networking,
a read-only quarantined input, and only the designated output path writable.
If required sandbox properties are unavailable in the LXC, PDF uploads must
fail rather than falling back to a direct converter invocation.

Install compatible GEOS/GDAL libraries for GeoDjango/PostGIS and configure
`GDAL_LIBRARY_PATH` where normal discovery cannot find GDAL. Nginx
`client_max_body_size` (300m) is the outer request-size boundary and sits modestly
above the application's precise aggregate file/archive ceiling
`MAX_INGEST_UPLOAD_BYTES` (256 MiB) so multipart framing overhead cannot clip a
near-limit upload; individual PDFs are bounded separately by `MAX_PDF_INPUT_BYTES`
in the application. Accepted fire plans
remain private under `/var/lib/fire-backend/fire-plans`; they are neither a
static directory nor an Nginx media alias.

## Installation and rollback

Use the repository deployment entrypoint and its documented required secrets;
do not run migrations as the runtime application user. Before declaring an
upgrade complete, run `nginx -t`, the deployment verifier, and the appropriate
[acceptance checklist](acceptance.md). Production-like acceptance is always
against one committed, pushed exact SHA: do not substitute an uncommitted tree
or a mutable release directory for that gate.

Switching `/srv/firedash/current` can roll back application code, but it does
not reverse database migrations or publication data. Plan and test database
rollback separately. Backups and restore drills are operational requirements.

### Activation and rollback prerequisites

The installer builds at the final SHA path and writes the completion marker only
after dependency, import, permission, and venv checks. Bootstrap installs the
units and reloads systemd without restarting running application services. Before
migration, quiescing stops timers and activation sockets, then database workers
and Gunicorn. A failed stop blocks migration. This includes the build wake socket
and backup jobs, even if a running backup timer is not enabled at boot.
Quiescing now precedes secret initialization as well as migrations: the missing
Report Transfer keyring safety check must not race a report acceptance. A failed
credential check leaves maintenance in effect; restore the credential and rerun
the same installer, rather than manually restarting an old release.

Activation requires a completed release and a valid final-path Gunicorn
interpreter. It then runs owner-role migrations, reapplies runtime/backup grants,
collects static files, and runs `check --deploy` and `migrate --check` before
atomically replacing `current`. Failure before the switch preserves that link;
it can still leave a changed database, static files, configuration, and stopped
services. After the switch, health, timers, administrator initialization, and the
full deployment verifier must succeed before the last-successful SHA is recorded.
Neither a completion marker nor a switched link alone means deployment succeeded.
The verifier includes Django-check failures in its final exit status.

Before migrating an established installation, record and verify:

- The active SHA, its completed immutable release and working venv, and the
  proposed exact SHA. Retain the old release and its compatible systemd/config
  requirements; those files are host configuration, not switched by `current`.
- The migration plan (`showmigrations --plan` using the proposed release and
  owner-role environment), including data migrations and compatibility with the
  old application's queries and writes. `migrate --check` proves no pending
  migrations for that code; it does **not** prove backward compatibility.
- A successful pre-upgrade database backup and retained snapshots of accepted
  sources, publications, configuration and credentials, with snapshot identifiers,
  decryption material and a tested restore procedure. Merely having a backup
  timer configured, or passing the verifier's throwaway `pg_dump`, is insufficient.
- Whether rollback will use compatible code against the migrated schema, tested
  reverse migrations, or a coordinated database/files/credentials restore. Review
  writes made since the backup and retain historical publication signing keys.

If compatibility or restore prerequisites cannot be established, do not switch
back to old code or describe rollback as automatic/safe. Keep maintenance in
place and converge forward or execute the reviewed restore plan. The installer
does not reverse migrations or automatically restart old code on failure.
`fire-restore` restores only files into an empty resolved child of
`/var/lib/fire-backend/restore-tests`; it does not restore a database. Traversal,
symlink escapes, and restoring onto the restore root itself are rejected.

### Deployment hardening audit (2026-10-19)

The review covered `install.sh`, `install-local.sh`, `bootstrap-lxc.sh`, release,
systemd, convergence and PostgreSQL helpers, unit definitions, the sanitizer
broker/client/storage boundary, deployment verification, and backup/restore
helpers. Owner-only schema changes, runtime grants/audit restrictions, root-owned
non-writable release code, credential separation, and the existing service
isolation directives were retained. They still require verification on Linux;
local command fixtures do not establish actual service permissions or isolation.

Confirmed failures and focused regressions:

| Invariant / failure scenario | Correction and regression |
| --- | --- |
| Direct activation could run an unfinished release without a completion marker. | Check completion/building markers and the venv before mutable work; `test_release_activation.sh` also injects migration, grants, static collection and deployment-check failures and checks that `current` stays unchanged. |
| A live build wake socket could restart a stopped worker during migrations; a running disabled backup or a failed timer stop could escape quiescing. | Stop activation sources before workers, stop backups regardless of enablement, and fail on stop errors; `test_systemd_quiesce.sh` checks ordering, repeated runs and failures. |
| Legacy publication-worker retirement failures were ignored, allowing conflicting worker lanes. | Propagate disable/stop failures; covered by `test_systemd_quiesce.sh`. |
| The verifier logged failed Django checks in a subshell but could still report overall success. | Propagate the subshell result to the parent failure counter; `test_verify_django_checks.sh` executes the actual block with failing check/migration fixtures. |
| The broker followed symlinked output directories despite `O_NOFOLLOW` on the filename; a FIFO could block before `fstat`. | Open root/job directories without following links, then finalize relative to pinned descriptors; use a nonblocking file open and reject empty/multiply linked files. Broker tests cover descriptor cleanup and rejection, plus Linux kernel tests for symlinks, hard links, FIFO and directory replacement. |
| A lexical restore prefix accepted `restore-tests/../outside` and symlink escapes. | Resolve the target before containment checks; `test_restore_target.sh` checks traversal, root/nonempty rejection and native symlink escapes. |
| Activation reran the account helper, changing the mutable parent from tmpfiles' `root:root 0711` to application-owned `0710`, allowing sibling trees to be renamed by the web identity. | Match the existing root-owned tmpfiles contract in `create-service-users.sh`; a deployment-lane regression checks both definitions. Linux negative rename checks remain required. |

Executed locally on Windows: 19 deployment helper scripts passed using isolated
fixtures/static assertions (including four new scripts); the existing root-only
rotation integration script skipped. Focused broker/client and publication-unit
tests passed (48 tests), with six Linux filesystem cases skipped. Bash syntax,
Python lint/format and diff checks were also performed. Git Bash could not run
native symlink activation/restore checks or positive Unix-mode restore handling;
WSL was inaccessible. No live upgrade, rollback, restart or key rotation was run.

Package 4 adds the four portable helper scripts to ordinary CI and keeps the
privileged rotation helper excluded. The full serial regression, locale-contract
correction and per-environment skip accounting are documented in
[development.md](development.md#ci-postgresql-locale-contract). Supplying the
missing locale spelling is confined to the disposable CI PostgreSQL container;
production SQL and compatibility checks remain unchanged. Linux CI should run
the native filesystem/symlink branches, but does not replace the installed-host
checks below. No Package 4 result authorizes a testserver installation or rollback
drill.

Perform these checks later on the controlled Debian 13 testserver:

1. In an isolated checkout with the test PostgreSQL settings configured, run
   `DJANGO_SETTINGS_MODULE=config.settings.test .venv/bin/python -m pytest -p no:cacheprovider apps/reference_data/tests/test_pdf_sanitizer_broker.py apps/reference_data/tests/test_pdf_sandbox.py apps/publications/tests/test_deployment_lanes.py`.
   All six native filesystem cases must execute. Run the four new
   `deploy/tests/test_{release_activation,systemd_quiesce,verify_django_checks,restore_target}.sh`
   scripts with Bash; native activation and restore branches must execute.
   The fixtures model systemctl calls; they are not systemd integration tests.
2. On the installed host run `systemd-analyze verify /etc/systemd/system/fire-*.service /etc/systemd/system/fire-*.socket /etc/systemd/system/fire-*.timer`,
   `nginx -t`, and the exact-SHA `deploy/verify-deployment.sh`. Confirm release
   ownership/modes, socket modes, negative credential access, document-reader
   access and the owner/runtime/backup SQL denials using the existing verifier.
   Confirm `/var/lib/fire-backend` stays `root:root 0711` after activation and
   reboot/tmpfiles; `fire_backend` and `fire_publication` must be unable to write
   that parent or rename sibling trees, while their intended child trees remain
   writable. Run native rename probes only against disposable fixture directories.
3. Run the real sanitizer smoke command as the application identity with the
   runtime environment, for example as root in this controlled installation:

   ```sh
   systemd-run --wait --pipe --collect --unit=fire-library-pdf-verification \
     --property=User=fire_backend --property=Group=fire_backend \
     --property=SupplementaryGroups=fire_document_readers \
     --property=EnvironmentFile=/etc/fire-backend/fire-backend.env \
     --setenv=DJANGO_SETTINGS_MODULE=config.settings.production \
     --working-directory=/srv/firedash/current \
     /srv/firedash/current/venv/bin/python manage.py verify_library_pdf_sanitizer
   ```

   Confirm benign output is accepted and malformed/password-protected inputs are
   rejected. Inspect the real job's private network, bind mounts, timeout/failure
   propagation, output ownership/mode and quarantine/output cleanup. Confirm the
   broker receives no application environment/credentials. Unit text alone does
   not prove that the LXC supports these sandbox directives.
4. Use a disposable test installation and dedicated failing test releases to
   exercise interrupted builds, migration/pre-switch failures, and a failed
   post-switch health check. Confirm the previous link survives pre-switch
   failures and maintenance remains in place where migrations may have run.
   Rerun the exact-SHA installer to verify socket/timer/service convergence:
   sanitizer socket, build wake socket/timer, delivery, maintenance, import
   staging, assignment expiry and stale-installation jobs. Ensure the legacy
   worker timer is disabled/inactive and the worker stopped, with no competing
   database jobs during migration. Do not edit immutable deployed releases.
5. Verify backup snapshot identifiers and rehearse the reviewed rollback/restore
   plan against a separate database and isolated files. The privileged rotation
   helper regression belongs in an isolated root-capable Linux fixture, not
   against active credentials. Neither this audit nor a symlink switch certifies
   schema rollback compatibility for a future release.


## Tablet storage telemetry disabled

Tablet storage telemetry is currently disabled. The iPad app manages device
storage locally, checks available space before downloading and activating updates,
and preserves previously verified data when an update cannot fit. Offline lease
and authorization requirements still apply. The server receives no automatic
device-capacity measurements or derived insufficient-storage reports. No diagnostic
report API is provided by this change. Future MDM-based inventory or capacity
monitoring is a separate project.

Source-library quotas, department/station quota policies, PDF size restrictions,
source usage accounting, and server filesystem safeguards still apply.

Capacity review pages and approval controls have been removed. Historical database
fields/tables and audit records are retained without active reads or writes; no
cleanup migration or live data edit is required. Deploy the application and restart
all publication workers together to avoid old workers continuing capacity checks.
Previously capacity-blocked PENDING jobs proceed on their existing schedule using
current source snapshots at claim time. Clean source is reconciled without forcing
rebuilds; explicit rebuild requests and scope authorization remain enforced. Normal
source-library quotas remain configurable in System Settings. See
[file-libraries.md](file-libraries.md#device-storage-is-managed-locally).

### Combined Dangerous Goods deployment

Deploy the SYSTEM ownership change together with Phase 2. Stop publication,
manifest, and ingestion workers and prevent writes during the migration. Before
running `ingestion.0005_system_dangerous_goods`, capture the legacy paths from
`publications_datasetpublication.artifact_path` for `scope_type = 'DEPARTMENT'`
and `dataset_type_code = 'dangerous_goods'`, and the `staging_key` values from
`ingestion_importbatch` for `domain = 'dangerous_goods'`. Keep the inventory in
a protected deployment location outside the repository. The paths are relative
to `PUBLICATION_ARTIFACT_ROOT` and `INGESTION_STAGING_ROOT`, respectively.
After the combined deployment has completed and the inventory has been checked
against those roots, remove the listed legacy files using the deployment's
normal artifact-cleanup process. The migration does not delete files.

The migration removes legacy department Dangerous Goods database records and
specifically attributable audit events. It temporarily suspends the audit and
publication-activation immutability triggers only while holding their table
locks in the migration transaction; the migration role must own those tables.
The triggers are restored before commit. Do not grant the runtime role extra
permissions. A verified backup and the captured inventory are needed before
migration because the legacy data is not copied to SYSTEM.

## Automatic Report Transfer infrastructure (Phase 3.6)

The standard `deploy/install.sh` command now provisions Report Transfer without
an enablement flag or a separate installer. The previous failure scenario—a
generally healthy installation advertising `report_transfer.enabled=false`
because its dedicated infrastructure was absent—is now a deployment failure.

- `/etc/fire-backend/credentials/report-transfer-keyring.json` is a durable,
  root-owned `0600` JSON credential. Its `keys` map contains immutable version
  identifiers and PEM RSA private keys. Initial keys are RSA-3072, unencrypted
  PKCS#8 inside the protected credential; generation uses the release's installed
  `cryptography`. Validation uses the application's actual parser and derives the
  active public key without printing it.
- `REPORT_TRANSFER_ACTIVE_KEY` defaults to `1` and is preserved through environment
  rendering. All existing key versions and valid file bytes are preserved. The
  installer never rotates keys. Keep historical private versions until retained
  ciphertext no longer needs them. No private RSA material belongs in the env file.
- Gunicorn receives the keyring only through systemd `LoadCredential` and
  `CREDENTIALS_DIRECTORY`. It retains `/var/lib/fire-backend` write access and adds
  `/var/spool/firedash-reports`; publication private keys remain excluded.
- `/var/spool/firedash-reports` is `fire_backend:fire_backend 0700`. Bootstrap
  validates an existing directory before tmpfiles can converge it. Symlinks,
  unexpected types, noncanonical parent paths, wrong ownership or permissions
  stop installation. Existing ciphertext and download receipts are never removed,
  moved or recursively chmodded by deployment.
- `fire-report-expiry.timer` starts approximately five minutes after boot and five
  minutes after its service last activated. The oneshot runs
  `/srv/firedash/current/venv/bin/python manage.py expire_report_submissions` as
  `fire_backend`, with production settings and the runtime database environment.
  It has no RSA credential and its only writable exception under `ProtectSystem=strict`
  is the report spool. It participates in quiesce, reruns and timer activation.

Before secrets and migrations, the installer stops all database writers, including
the expiry timer/service and web activation socket. If the keyring is absent,
generation requires proof that the spool is empty, the feature-initialized marker
is absent, and the database either does not exist, has no report records, or has
no Report Transfer table/migration history yet. Catalog/connection errors and
missing previously migrated tables fail closed. Any report row, including an
expired historical row, requires restoring the original missing keyring. The
root-only `/etc/fire-backend/report-transfer-initialized` marker also prevents
silent regeneration after a later credential loss. An interrupted generation can
be safely rerun; an installed valid keyring is always reused.

The exact old documented web drop-in is removed when installing canonical units.
Mixed/conflicting report overrides are preserved and stop the installer with the
filename for review; unrelated administrator overrides are retained. Custom
`REPORT_TRANSFER_ROOT` or direct `REPORT_TRANSFER_KEYRING_PATH` environment values
also require explicit migration review rather than silently abandoning data.

The verifier checks effective systemd properties, private storage/access, timer
installation/enablement/schedule, credential separation, HTTPS origin and Nginx
bearer-route logging suppression. It also enters the running Gunicorn master's
mount namespace, drops to `fire_backend`, and validates the actual process
environment, credential and enabled version-1 RSA-OAEP-256/A256GCM/JPEG configuration.
It does not use a tablet token, create a report or send mail. Failure to inspect
the namespace (including missing Linux capabilities) is a verification failure,
not permission to fall back to an insecure credential path.

### Backup and restore boundary

The existing protected credential backup includes the dedicated keyring because
it backs up `/etc/fire-backend/credentials`. Back up that durable secret and test
its restoration together with the matching database. Never replace a lost keyring
with freshly generated keys to make a retained report readable.

Normal FireDash file backups explicitly select database dumps, accepted fire
plans, publications and credentials. They do **not** select the report spool or
its receipts. Database dumps still contain report metadata, not payload files.
Hypervisor/LXC/volume snapshots and external backup agents are outside the
installer's control: operators must exclude the spool, including receipts, from
those retention paths. A full filesystem snapshot containing it does not satisfy
the temporary-data boundary. The installer cannot guarantee infrastructure-level
exclusions. Existing report retention and cleanup semantics remain unchanged.

### Read-only troubleshooting

On the host, these commands disclose metadata rather than private key contents:

```bash
sudo stat -c '%U:%G:%a %n' /etc/fire-backend/credentials/report-transfer-keyring.json /var/spool/firedash-reports
sudo systemctl show fire-backend.service -p User -p Group -p LoadCredential -p ReadWritePaths -p MainPID
sudo systemctl show fire-report-expiry.service -p User -p Group -p ExecStart -p EnvironmentFiles -p ReadWritePaths -p LoadCredential
sudo systemctl show fire-report-expiry.timer -p Unit -p TimersMonotonic -p Persistent -p ActiveState -p UnitFileState
sudo systemctl list-timers fire-report-expiry.timer --all
sudo journalctl -u fire-report-expiry.service -n 20 --no-pager
```

Using an already-adopted Fake iPad, a configuration GET needs no re-adoption,
dataset sync, report upload or email. Use its existing server and state directory:

```powershell
$config = .venv/Scripts/python.exe -m tools.fake_ipad configuration --server https://fire.example.org --state-dir C:\private\existing-ipad --json | ConvertFrom-Json
$config.report_transfer | Select-Object enabled, version, kid, alg, enc, max_attachments
```

Expect `enabled=True`, `version=1`, the preserved active `kid`, `RSA-OAEP-256`,
`A256GCM` and a positive attachment limit. Do not print the entire configuration
unnecessarily. A working verified outbound-mail provider remains an existing
application prerequisite; deployment verification sends no probe email.

### Operator-controlled redeployment after review

No deployment was performed during this local implementation. After the changes
are reviewed and released at a committed, pushed exact SHA, an operator should:

1. Verify the protected credential/database backups and external spool snapshot
   exclusions; review existing custom Report Transfer overrides and paths.
2. On the target host, run the normal installer for that approved SHA:

   ```bash
   sha='<approved-40-character-commit-SHA>'
   curl -fsSL "https://raw.githubusercontent.com/dermetti/FireDash-Server/$sha/deploy/install.sh" | sudo bash -s -- --ref "$sha" --wait
   ```

   Established installations reuse their saved hostname/TLS configuration. There
   are no new setup flags, interactive key generation or manual unit installation.
3. Require the installer's final verification success. If credentials are invalid
   or missing, restore/repair the original material and rerun the same command.
   Do not delete the spool, regenerate keys or manually bypass maintenance.
4. Check the timer and adopted-tablet configuration above. A separate operator
   acceptance may later exercise real email/report downloads; it is not performed
   by the deployment verifier.

Required Linux and real-email evidence is tracked in
[acceptance.md](acceptance.md#report-transfer-beta-acceptance). Portable helper
success does not establish native systemd/LXC isolation.


### Debian systemd property rendering

Report Transfer verification does not require printable complex `systemctl show`
properties. Debian may display `LoadCredential=[unprintable]`. The installer
privately checks the effective unit fragment and drop-ins, requires
`NeedDaemonReload=no`, and retains the running web-process credential probe.
Missing/conflicting declarations, unknown configuration or a failed probe still
stop verification. Do not dump credential contents when troubleshooting.
The effective-unit parser must honor ordered drop-ins, list resets,
continuation lines, quoted arguments and final scalar overrides. Unsupported
escaping/specifiers fail closed. Require the canonical credential mapping;
reject missing, duplicate, conflicting or inappropriately imported report
credentials, including worker leakage. Verify spool write paths, exact expiry
command/environment and five-minute timer configuration. Scalar runtime
properties must be readable (plain or `Property=value`); missing/opaque values
fail verification. Never print unit contents or credential values.

The running web-process mount-namespace probe must derive the advertised active
public key from the systemd-delivered keyring without tablet tokens, reports or
email. Portable fixtures for printable, empty or `[unprintable]` properties do
not certify native symlink, root/service-identity ownership/access or real
systemd credential isolation. Require isolated native Debian/LXC acceptance
before operational approval; Windows/Git Bash cannot establish those guarantees.

### Dependency advisory review and release approval

Dependency advisories are tracked maintenance findings, not automatic CI failures.
Every standard CI run has an independent, non-blocking `dependency-audit` job.
It scans both pinned requirements files with dependency resolution, publishes
`dependency-advisories` JSON/Markdown artifacts and a job summary, and reports
advisory-entry and affected-package counts separately for each scan. Counts can
overlap between scans and advisory aliases. Each scan has a four-minute limit.
Timeouts, resolution/service errors and skipped dependencies are reported as
**INCOMPLETE**, with unknown totals; they must never be interpreted as zero
vulnerabilities. Tool-installation failures are also reported in the job summary.
Raw resolver logs and environment values are not published.

The independent mandatory jobs `static-checks`, `server-tests` and
`deployment-security` retain deployment tests, Django checks, migration drift,
Ruff, mypy, Bandit, the full PostgreSQL/PostGIS regression suite and OpenAPI
validation. Their success does not mean the advisory scan is clean or that
dependencies are secure. No blanket advisory ignores are applied.

Review each advisory for severity (unknown severity is not harmless), affected
functionality used by FireDash, reachability from untrusted inputs, patched-version
availability, compatibility and regression results. Confirmed exploitable
critical/high-severity vulnerabilities in reachable FireDash functionality must
block production release approval through an explicit release decision. Require
green quality checks independently of this review. This policy triggers no
automatic deployment or credential operation.

For local reproduction, run `python tools/dependency_audit.py`. This executes
`pip-audit -r requirements/base.txt` and `pip-audit -r requirements/dev.txt` with
JSON output and the same timeout/reporting rules. If resolution cannot complete,
`pip-audit --local` audits the actual installed environment, including transitives,
but is not proof of a fresh requirements resolution. A supplementary
`pip-audit -r requirements/base.txt --no-deps --disable-pip` checks direct pins
only; scan dev separately, since this mode does not include its nested base file.
Always record the scan mode and incomplete results.

See [historical unresolved dependency findings](security.md#historical-unresolved-dependency-findings-2026-11-02)
for the dated local-environment observations and outstanding advisory review.
