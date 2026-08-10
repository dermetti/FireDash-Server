# Debian LXC Deployment

Run `sudo ./deploy/bootstrap-lxc.sh` on a clean Debian 13 amd64 LXC before the first release. It installs host dependencies, users, PostgreSQL 17/PostGIS, host units, the PDF sudo boundary, and a deploy key, but does not deploy code, run migrations, create an administrator, enable FireDash services, or configure TLS. Add the printed public key to GitHub, then rerun with `--repository-url git@github.com:<owner>/<repository>.git` to verify read-only access. Supply initial role passwords only through root-owned mode `0600` `/etc/fire-backend/bootstrap-postgresql.env`; no secrets belong in this repository.

1. Create the `fire_backend` service account with `deploy/scripts/create-service-users.sh` as root.
2. Install the application at `/opt/fire-backend` and create its virtual environment at `/opt/fire-backend/venv`.
3. Install `requirements/base.txt`; do not run Django or Gunicorn as root.
4. Install the systemd unit, socket, and tmpfiles configuration. Run `systemctl daemon-reload` and `systemd-tmpfiles --create`, then run `systemctl enable --now fire-backend.socket`. Do not enable `fire-backend.service`: it is socket-activated at runtime, and Gunicorn inherits the systemd-created Unix socket.
5. Create the root-owned environment file described in `configuration.md`.
6. Install `deploy/systemd/fire-stale-installation.{service,timer}`, `fire-backup.{service,timer}`, and `fire-restore.service` with the existing units. Enable `fire-publication-worker.timer`, `fire-temporary-assignment-expiry.timer`, `fire-stale-installation.timer`, and `fire-backup.timer`. `fire-restore.service` is manual-only.
7. Copy the Nginx site configuration, replace `fire-backend.internal` and certificate paths, test it with `nginx -t`, then reload Nginx.
8. On Debian 13, provision PostgreSQL 17 with PostGIS 3.5 using `deploy/postgresql/bootstrap-production.sql`; do not run the CI/development-only `bootstrap-test.sql` on the host. Apply migrations as `database_owner`, then apply `deploy/postgresql/roles.sql` as `database_owner`, run `collectstatic`, and verify `https://<host>/health/live` and `/health/ready`.

Gunicorn only listens on its Unix socket. Nginx is the TLS endpoint. PostgreSQL listens locally only. Do not use Django's development server in production.

## Publication artifacts

The publication worker runs as `fire_publication`, not the web-service account. Create the
root-owned, mode `0700` `/etc/fire-backend/credentials` directory and root-owned, mode `0600`
credential sources beneath it: `publication-kek`, `publication-signing-key`, and
`publication-signing-public-key`. The KEK and private signing key each contain either raw or
base64-encoded 32-byte key material. `LoadCredential=` exposes the KEK and private key only to
`fire-publication-worker.service`; it exposes only the public key to `fire-backend.service`.
Never put these values in `fire-backend.env` or grant service users direct access to the source
credential directory.

Artifacts are encrypted and written by the worker under `/var/lib/fire-backend/publications`.
The final directory is `fire_publication:fire_nginx` mode `2750`; final ciphertext files are mode
`0640` in that group so Nginx's `www-data` identity can serve only authorized X-Accel requests.
`/var/lib/fire-backend/publications-tmp` remains `fire_publication:fire_publication` mode `0700`.
The web service has neither credentials nor filesystem ownership there. Set artifact roots, size
limit, stale timeout, and KEK version through the `PUBLICATION_ARTIFACT_*` and
`PUBLICATION_KEK_VERSION` settings before starting the worker.

Static assets remain at `/var/lib/fire-backend/static` for the MVP. Run `collectstatic` from the
deployment/root context so static directories are mode `0755` and files are mode `0644`; Gunicorn
does not own static deployment. The dedicated non-secret `fire_nginx` group contains only
`www-data` and `fire_publication`. It does not grant access to application configuration,
credential sources, accepted plans, quarantine data, or publication temporary files.

## PDF sanitizer

Install the root-owned `0755` launcher at `/usr/local/lib/fire-backend/fire-pdf-sanitize`, the
root-owned `0440` sudoers file `deploy/sudoers/fire-pdf-sanitizer`, and
`fire-pdf-sanitizer@.service`. Django invokes only `sudo -n` plus a generated UUID; the wrapper
starts only the matching fixed template unit and finalizes only its expected output. The template
binds that job's `quarantine/<uuid>/input.pdf` read-only and `sanitizer-output/<uuid>` writable,
while making plans, artifacts, credentials, and release paths inaccessible. The sanitizer-output
root is `2710`, so only backend-created `2730` job directories are writable by the sanitizer group.
Validate transient bind-mount and cgroup behavior on the Debian 13 LXC before accepting uploads in
production. In particular, prove a job A instance cannot read job B input or read/modify job B
output; the template's fixed bind mounts must be validated as the per-job isolation boundary.

The protected-download endpoint maps each new artifact to `<publication_uuid>.bin` in this root.
Rebuild existing publications created with the former nested artifact layout before enabling the
Phase 9 Nginx configuration.

## Backup installation

Install `restic`, PostgreSQL client tools, and a root-owned `/etc/fire-backend/backup.env` with
mode `0600`. It must set only the backup database connection metadata, `RESTIC_REPOSITORY`, and
optional retention values (`RESTIC_KEEP_DAILY`, `RESTIC_KEEP_WEEKLY`, and
`RESTIC_KEEP_MONTHLY`). Store repository-provider credentials only in this root-only file or its
provider-supported credential mechanism; never in this repository or the application environment.

Create root-owned, mode `0600` credential files at
`/etc/fire-backend/credentials/backup-pgpass` and
`/etc/fire-backend/credentials/restic-password`. The first is a PostgreSQL password file for a
least-privileged `backup_role`; the second is the restic repository password. The backup service
receives both as systemd credentials and backs up a custom-format PostgreSQL dump, sanitized fire
plans, encrypted publication artifacts, and the protected credential directory. Configure the
restic repository to use at least one destination outside Proxmox.

Create `/var/lib/fire-backend/restore-tests` as root, mode `0700`. A restore test is deliberately
manual: set `FIRE_RESTORE_SNAPSHOT` and a new empty
`FIRE_RESTORE_TARGET` below that directory in root-owned `/etc/fire-backend/restore.env`, then run
`systemctl start fire-restore.service`. The service restores files only and never runs `pg_restore`
or changes the production database. Validate a database dump only in an isolated PostgreSQL
instance following the procedure in `docs/operations.md`.
