# Configuration reference

This is the canonical runtime setting reference. The settings modules and
installer are authoritative for names and defaults. Environment values live in
`/etc/fire-backend/fire-backend.env`; secret material is delivered as systemd
credentials, not added to that file.

## Django and web

| Setting | Default / requirement | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | required in production | Secret. |
| `DJANGO_ALLOWED_HOSTS` | required in production | Comma-separated hosts. |
| `FIREDASH_PUBLIC_ORIGIN` | required in production | Non-secret HTTPS origin encoded in tablet provisioning QR payloads; no path or query. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | Comma-separated origins. |
| `DJANGO_STATIC_ROOT` | `/var/lib/fire-backend/static` | Static collection destination. |
| `ADMIN_SESSION_MAX_AGE_SECONDS` | `28800` | Browser-session lifetime. |
| `PRE_MFA_SESSION_MAX_AGE_SECONDS` | `600` | Pre-TOTP lifetime. |
| `AUTH_THROTTLE_MAX_FAILURES` | `5` | Login/TOTP throttle threshold. |
| `AUTH_THROTTLE_WINDOW_SECONDS` | `900` | Throttle window. |
| `AUTH_THROTTLE_LOCKOUT_SECONDS` | `900` | Throttle lockout. |
| `RECENT_REAUTH_MAX_AGE_SECONDS` | `900` | Fresh step-up-auth window. |
| `TRUSTED_PROXY_IPS` | `127.0.0.1,::1` | Addresses trusted to supply proxy metadata. |

## PostgreSQL

`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_HOST` are
required in production. `POSTGRES_PORT` defaults to `5432`. The runtime role
is intentionally different from the database-owner and backup roles described
in [deployment.md](deployment.md); their passwords are credentials, not
application environment settings.

## Reference data and PDF sanitisation

| Setting | Default |
| --- | --- |
| `REFERENCE_DATA_QUARANTINE_ROOT` | `/var/lib/fire-backend/quarantine` |
| `REFERENCE_DATA_SANITIZER_OUTPUT_ROOT` | `/var/lib/fire-backend/sanitizer-output` |
| `REFERENCE_DATA_ACCEPTED_ROOT` | `/var/lib/fire-backend/fire-plans` |
| `MAX_PDF_INPUT_BYTES` | 100 MiB |
| `MAX_PDF_OUTPUT_BYTES` | 150 MiB |
| `MAX_PDF_PAGES` | 500 |
| `MAX_HYDRANT_IMPORT_FEATURES` | 20,000 |
| `PDF_SANITIZER_TIMEOUT_SECONDS` | 60 |
| `PDF_SANITIZER_MEMORY_MAX_BYTES` | 512 MiB |
| `PDF_SANITIZER_BROKER_SOCKET` | `/run/fire-pdf-sanitizer-broker/broker.sock` |

Canonical ingestion stages source uploads privately before administrator
confirmation. The staging root must not be Nginx-served and must be writable
only by the application identity; it contains no publication credentials.

| Setting | Default |
| --- | --- |
| `INGESTION_STAGING_ROOT` | `/var/lib/fire-backend/import-staging` |
| `MAX_STRUCTURED_IMPORT_BYTES` | 20 MiB |
| `MAX_STRUCTURED_IMPORT_ROWS` | 20,000 |
| `MAX_IMPORT_VALIDATION_ERRORS` | 200 |
| `MAX_PDF_PACKAGE_BYTES` | 200 MiB |
| `MAX_PDF_PACKAGE_EXPANDED_BYTES` | 500 MiB |
| `MAX_PDF_PACKAGE_MEMBERS` | 1,000 |
| `IMPORT_PREVIEW_RETENTION_DAYS` | `7` |
| `IMPORT_APPLIED_SOURCE_RETENTION_DAYS` | `30` |

The broker socket is a local service boundary, not a public API.

## Publications

| Setting | Default |
| --- | --- |
| `PUBLICATION_WORKER_BATCH_SIZE` | `10` |
| `PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS` | `300` |
| `PUBLICATION_JOB_MAX_ATTEMPTS` | `3` |
| `PUBLICATION_BUILD_WAKE_SOCKET_PATH` | `/run/fire-backend/publication-build.sock` |
| `PUBLICATION_BUILD_WAKE_TIMEOUT_SECONDS` | `1.0` |
| `PUBLICATION_BUILD_SUMMARY_MAX_ITEMS` | `10000` |
| `SIGNED_MANIFEST_RETENTION_DAYS` | `30` |
| `PUBLICATION_ARTIFACT_ROOT` | `/var/lib/fire-backend/publications` |
| `PUBLICATION_ARTIFACT_TEMP_ROOT` | `/var/lib/fire-backend/publications/.tmp` |
| `PUBLICATION_ARTIFACT_MAX_BYTES` | 100 MiB |
| `PUBLICATION_ARTIFACT_STALE_SECONDS` | `3600` |
| `PUBLICATION_KEK_VERSION` | `1` |
| `PUBLICATION_SIGNING_KEY_VERSION` | `1` |

The artifact temporary root must be a strict descendant of the artifact root
so final promotion is an atomic same-filesystem rename. Normal source edits
coalesce for the nightly build rather than a sliding debounce.

Publication credentials are normally resolved from systemd's per-service
`CREDENTIALS_DIRECTORY`; do not configure a unit-name-specific credential path.
The explicit `PUBLICATION_KEK_CREDENTIAL_PATH`,
`PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH`, and
`PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH` overrides exist only for
development and focused tests. Production loads a root-managed JSON public-key
ring credential with exact version-to-standard-Base64 raw-Ed25519-key entries.
The web service receives only this public ring; it never receives a private
signing key or the KEK.

The installer maintains `/etc/fire-backend/credentials/publication-signing-public-key-ring.json`
as root:root mode `0600`. Its deliberately small format is:

```json
{"keys":{"1":"<standard-Base64 raw 32-byte Ed25519 public key>","2":"<...>"}}
```

Every key version matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. The active
`PUBLICATION_SIGNING_KEY_VERSION` must exist in the ring and match the public
key derived from the worker-only active private seed. Installer reruns retain
unrelated historical entries; they never prune the ring.

Rotate the active signing pair only with the root-only two-phase
`/srv/firedash/current/deploy/rotate-publication-signing-key` helper. It preserves this public ring,
keeps active private credentials root-only, and changes only
`PUBLICATION_SIGNING_KEY_VERSION` in `fire-backend.env`; see the operational
[rotation runbook](operations.md#publication-signing-key-rotation). The KEK is
not a signing-key rotation input.

## Tablets, backups, and restore

Tablet lease duration belongs to each department, not an environment variable:
the default is seven days and the minimum is three days. Automatic check-in
renewal occurs only with 48 hours or less remaining; authenticated tablet
refresh explicitly tops up an eligible active installation.

Backup and restore location, scheduling, and role credentials are deployment
concerns. See [deployment.md](deployment.md) and the operational procedures in
[operations.md](operations.md).
