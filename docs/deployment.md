# Debian LXC Deployment

FireDash installs onto a clean Debian 13 (trixie) amd64 LXC with a single command:

```text
curl -fsSL https://raw.githubusercontent.com/dermetti/FireDash-Server/main/deploy/install.sh | sudo bash
```

Production documentation should pin an immutable release tag or commit SHA rather than the
mutable `main` branch:

```text
curl -fsSL https://raw.githubusercontent.com/dermetti/FireDash-Server/<tag>/deploy/install.sh | sudo bash
```

## What the installer does

The installer is a two-stage, idempotent process:

1. **Stage 0 (`deploy/install.sh`)** — a self-contained bootstrapper that verifies Debian 13,
   ensures `curl`/`git`/`ca-certificates` are present, fetches the public repository over HTTPS,
   resolves and pins the exact Git SHA, and delegates to the repository-local installer.
2. **Stage 1 (`deploy/install-local.sh`)** — the orchestrator that runs all host, release,
   secret, PostgreSQL, Nginx, systemd, and application phases and finishes with full verification.

The installer:

- installs OS dependencies, service identities, and systemd units
- builds an immutable, root-owned release under `/srv/firedash/releases/<sha>`
- generates the initial secret set on first install (never rotated on rerun)
- bootstraps PostgreSQL roles, runs migrations as `database_owner`, and reapplies grants
- configures Nginx to terminate TLS using externally managed certificates
- activates Gunicorn via socket activation and enables background timers
- creates the initial system administrator with a one-time setup URL

It does **not** own or configure TLS/ACME or Tailscale/Headscale. It only accepts and validates
existing certificate/key paths.

## Interactive prompts

On first installation, the installer prompts (via the terminal, not the piped script) only for
values it cannot generate:

- FireDash HTTPS base URL (e.g. `https://firedash.de`)
- TLS full-chain certificate path
- TLS private-key path
- initial system administrator email and display name

For automation, the same values can be supplied as environment variables:

```text
FIREDASH_BASE_URL=https://firedash.mjblab.de
FIREDASH_TLS_CERT_PATH=/etc/letsencrypt/live/firedash.mjblab.de/fullchain.pem
FIREDASH_TLS_KEY_PATH=/etc/letsencrypt/live/firedash.mjblab.de/privkey.pem
FIREDASH_INITIAL_ADMIN_EMAIL=admin@example.com
FIREDASH_INITIAL_ADMIN_DISPLAY_NAME="System Administrator"
```

`FIREDASH_REF` (or `--ref`) selects the branch, tag, or exact 40-character SHA to install; it
defaults to `main`.

## Installation state

`/etc/fire-backend/install.conf` holds non-secret configuration and the last successful SHA.
`/etc/fire-backend/secrets-initialized` marks a committed secret set. The installer distinguishes
a pristine host from a partially or fully installed one; on an established install, missing or
corrupt secrets fail closed rather than being regenerated.

## Credential isolation

The privilege model is unchanged:

- Gunicorn/web receives only the Ed25519 **public** verification key.
- `fire_publication` worker receives the KEK and private signing key via systemd `LoadCredential`.
- `database_owner` and `backup_role` passwords live only in root-owned `0600` credential files.
- The runtime `fire-backend.env` (`root:fire_backend 0640`) holds only the `application_runtime`
  database password, `DJANGO_SECRET_KEY`, and non-secret runtime settings.

## Publication artifacts

Publication artifacts are written under `/var/lib/fire-backend/publications`
(`fire_publication:fire_nginx`, mode `2750`; files `0640`) and served only through the authorized
X-Accel-style internal location. Static assets remain at `/var/lib/fire-backend/static` with
non-hashed filenames; `collectstatic` runs during the maintenance window before the release switch.

## PDF sanitizer

The installer preserves the root-owned `fire-pdf-sanitizer-broker` executable, the
`fire-pdf-sanitizer-broker.socket` activation unit (socket `root:fire_backend` mode `0660`,
parent directory root-owned and not writable by `fire_backend`), and the hardened
`fire-pdf-sanitizer-broker@.service` per-connection template (`Accept=yes`; systemd passes the
accepted connection on stdin/stdout). The broker spawns the transient
`fire-pdf-sanitizer@.service` sandbox (`PrivateNetwork=yes`, strict filesystem access, resource
limits). Neither template is enabled directly; instances are transient. The broker socket is
enabled via `activate_socket`. The application never elevates privileges (`fire-backend.service`
keeps `NoNewPrivileges=true`) and talks to the broker over a Unix socket; there is no sudo path.

## Backup

`restic`, PostgreSQL client tools, and the backup units are installed. The backup timer is enabled
only when the backup environment and credential files (`backup-pgpass`, `restic-password`) exist.
`fire-restore.service` remains manual-only.

## Verification

`deploy/verify-deployment.sh` runs automatically at the end of installation and can be invoked
manually. It checks host prerequisites, PostgreSQL roles and hardening, audit append-only and
protected-registry enforcement, backup role behavior, application checks, systemd state, credential
and filesystem isolation, and HTTPS health via a DNS-independent local `--resolve` request.

## Rollback

`/srv/firedash/current` is a symlink to the active release. Switching back is a code-only rollback:

```text
ln -sfn /srv/firedash/releases/<previous-sha> /srv/firedash/current
systemctl restart fire-backend.service
```

Database migrations are not automatically rolled back. FireDash follows an expand/contract
migration discipline so that a code rollback remains compatible with the current schema; a
destructive migration requires a documented maintenance/recovery procedure.
