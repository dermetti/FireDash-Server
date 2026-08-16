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
  cleanup without the KEK or private signing key.

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

## Reference-data sandbox

The root-owned PDF sanitizer broker remains outside the web process. Its Unix
socket is accessible to the web service only, accepts a canonical job UUID,
and launches a per-job `fire_pdf_sanitizer` sandbox with private networking,
a read-only quarantined input, and only the designated output path writable.
If required sandbox properties are unavailable in the LXC, PDF uploads must
fail rather than falling back to a direct converter invocation.

Install compatible GEOS/GDAL libraries for GeoDjango/PostGIS and configure
`GDAL_LIBRARY_PATH` where normal discovery cannot find GDAL. Keep Nginx
`client_max_body_size` aligned with `MAX_PDF_INPUT_BYTES`. Accepted fire plans
remain private under `/var/lib/fire-backend/fire-plans`; they are neither a
static directory nor an Nginx media alias.

## Installation and rollback

Use the repository deployment entrypoint and its documented required secrets;
do not run migrations as the runtime application user. Before declaring an
upgrade complete, run `nginx -t`, the deployment verifier, and the appropriate
[acceptance checklist](acceptance.md).

Switching `/srv/firedash/current` can roll back application code, but it does
not reverse database migrations or publication data. Plan and test database
rollback separately. Backups and restore drills are operational requirements.
