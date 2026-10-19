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

Outbound report admission uses the pinned `pypdf` package installed in the
normal FireDash virtual environment to inspect parsed encryption metadata. It
does not require an outbound-mail qpdf binary, vendor installation, or
deployment setting. The separate reference-data sanitizer remains qpdf-based.

## Optional outbound-mail HTTPS egress proxy

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
