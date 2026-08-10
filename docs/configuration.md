# Runtime Configuration

Use `/etc/fire-backend/fire-backend.env` in production. It must be owned by `root:fire_backend` with mode `0640`; it is not committed to the repository.

Required production settings:

```text
DJANGO_SECRET_KEY=<unique high-entropy value>
DJANGO_ALLOWED_HOSTS=fire-backend.internal
DJANGO_CSRF_TRUSTED_ORIGINS=https://fire-backend.internal
POSTGRES_DB=fire_backend
POSTGRES_USER=application_runtime
POSTGRES_PASSWORD=<runtime database password>
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
DJANGO_STATIC_ROOT=/var/lib/fire-backend/static
ADMIN_SESSION_MAX_AGE_SECONDS=28800
PRE_MFA_SESSION_MAX_AGE_SECONDS=600
AUTH_THROTTLE_MAX_FAILURES=5
AUTH_THROTTLE_WINDOW_SECONDS=900
AUTH_THROTTLE_LOCKOUT_SECONDS=900
RECENT_REAUTH_MAX_AGE_SECONDS=900
TRUSTED_PROXY_IPS=127.0.0.1,::1
REFERENCE_DATA_QUARANTINE_ROOT=/var/lib/fire-backend/quarantine
REFERENCE_DATA_SANITIZER_OUTPUT_ROOT=/var/lib/fire-backend/sanitizer-output
REFERENCE_DATA_ACCEPTED_ROOT=/var/lib/fire-backend/fire-plans
MAX_PDF_INPUT_BYTES=104857600
MAX_PDF_OUTPUT_BYTES=157286400
MAX_PDF_PAGES=500
MAX_HYDRANT_IMPORT_FEATURES=20000
PDF_SANITIZER_TIMEOUT_SECONDS=60
PDF_SANITIZER_MEMORY_MAX_BYTES=536870912
PDF_SANITIZER_WRAPPER=/usr/local/lib/fire-backend/fire-pdf-sanitize
PUBLICATION_WORKER_BATCH_SIZE=10
PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS=300
PUBLICATION_JOB_MAX_ATTEMPTS=3
TEMPORARY_ASSIGNMENT_EXPIRY_BATCH_SIZE=100
PUBLICATION_BUILD_SUMMARY_MAX_ITEMS=10000
PUBLICATION_ARTIFACT_ROOT=/var/lib/fire-backend/publications
PUBLICATION_ARTIFACT_TEMP_ROOT=/var/lib/fire-backend/publications-tmp
PUBLICATION_ARTIFACT_MAX_BYTES=104857600
PUBLICATION_ARTIFACT_STALE_SECONDS=3600
PUBLICATION_KEK_VERSION=1
PUBLICATION_SIGNING_KEY_VERSION=1
```

`DJANGO_SECRET_KEY`, database credentials, signing keys, key-encryption keys, and backup credentials must never be committed or logged. Publication credential source files are root-owned, mode `0600`, beneath the root-only `/etc/fire-backend/credentials` directory. systemd copies them into per-unit runtime credential directories; do not put them in this environment file.

`PUBLICATION_KEK_CREDENTIAL_PATH`, `PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH`, and
`PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH` default to the credential paths configured by
the systemd units. Do not set them to files readable by the web-service account. `DJANGO_DEBUG`
is development-only and defaults to `False` in production; do not set it in the production
environment file.

The publication worker receives only `PUBLICATION_KEK_CREDENTIAL_PATH` and
`PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH`, containing the AES-256 KEK and 32-byte Ed25519 private
seed. The web service receives only `PUBLICATION_SIGNING_PUBLIC_KEY_CREDENTIAL_PATH`, containing
the matching 32-byte raw Ed25519 public key. Its source is
`/etc/fire-backend/credentials/publication-signing-public-key`, loaded by
`fire-backend.service` as the `publication-signing-public-key` systemd credential. The web service
must not be able to read private credential sources or worker runtime credentials. Both deployments
use the same `PUBLICATION_SIGNING_KEY_VERSION`. The authenticated tablet endpoint
`/api/v1/tablet/signing-keys/{version}` distributes only the configured current public key.
Rotate the private key, public key, and version atomically; the current single-key configuration
does not retain prior public keys for historical artifact verification.

The non-secret `fire_nginx` group contains only `www-data` and `fire_publication`. It grants Nginx
read/traverse access to final encrypted publication artifacts, not publication temporary files,
accepted plans, application configuration, or credential sources.

Only the local Nginx proxy may appear in `TRUSTED_PROXY_IPS`. Nginx overwrites inbound `X-Forwarded-For`; application code must not trust a header received directly from a client.

## Phase 8/9 operational configuration

The stale-installation processor uses the production application environment and runs every 15
minutes. Lease duration is fixed at seven days in the application; do not treat the timer interval
as the authorization period.

`/etc/fire-backend/backup.env` is separate from `fire-backend.env`, owned by `root:root`, and mode
`0600`. Required names are `BACKUP_PG_DATABASE`, `BACKUP_PG_USER`, `BACKUP_PG_HOST`, and
`RESTIC_REPOSITORY`; `BACKUP_PG_PORT` and the three `RESTIC_KEEP_*` values are optional. It may
contain repository-provider credentials only when the provider requires environment variables.
Do not put a restic password or PostgreSQL password in it: those belong in the two systemd
credential files documented in `deployment.md`.

`/etc/fire-backend/restore.env` is root-owned, mode `0600`, and is created only for a planned
restore test. It contains `FIRE_RESTORE_SNAPSHOT` and `FIRE_RESTORE_TARGET`; the target must be a
new empty directory under `/var/lib/fire-backend/restore-tests`. Remove the file after the test.
