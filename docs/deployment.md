# Debian LXC Deployment

1. Create the `fire_backend` service account with `deploy/scripts/create-service-users.sh` as root.
2. Install the application at `/opt/fire-backend` and create its virtual environment at `/opt/fire-backend/venv`.
3. Install `requirements/base.txt`; do not run Django or Gunicorn as root.
4. Install the systemd unit, socket, and tmpfiles configuration. Run `systemd-tmpfiles --create`, then enable `fire-backend.socket` and `fire-backend.service`.
5. Create the root-owned environment file described in `configuration.md`.
6. Install `deploy/systemd/fire-stale-installation.{service,timer}`, `fire-backup.{service,timer}`, and `fire-restore.service` with the existing units. Run `systemctl daemon-reload`, then enable `fire-backend.socket`, `fire-publication-worker.timer`, `fire-temporary-assignment-expiry.timer`, `fire-stale-installation.timer`, and `fire-backup.timer`. `fire-restore.service` is manual-only.
7. Copy the Nginx site configuration, replace `fire-backend.internal` and certificate paths, test it with `nginx -t`, then reload Nginx.
8. Apply migrations as `database_owner`, run `collectstatic`, apply runtime grants, and verify `https://<host>/health/live` and `/health/ready`.

Gunicorn only listens on its Unix socket. Nginx is the TLS endpoint. PostgreSQL listens locally only. Do not use Django's development server in production.

## Publication artifacts

The publication worker runs as `fire_publication`, not the web-service account. Create
root-owned, mode `0640`, group `fire_publication` credentials at
`/etc/fire-backend/credentials/publication-kek` and
`/etc/fire-backend/credentials/publication-signing-key`. Each file contains either raw or
base64-encoded 32-byte key material. The KEK is AES-256 and the signing key is an Ed25519
private seed. `LoadCredential=` exposes them only to `fire-publication-worker.service`;
never put these values in `fire-backend.env` or grant the web service access to their directory.

Artifacts are encrypted and written by the worker under `/var/lib/fire-backend/publications`.
The web service has neither credentials nor filesystem ownership there. Set artifact roots,
size limit, stale timeout, and KEK version through the `PUBLICATION_ARTIFACT_*` and
`PUBLICATION_KEK_VERSION` settings before starting the worker.

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
