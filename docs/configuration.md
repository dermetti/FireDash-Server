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
| `MAX_HYDRANT_GEOJSON_FEATURES` | 50,000 |
| `MAX_IMPORT_VALIDATION_ERRORS` | 200 |
| `MAX_INGEST_UPLOAD_BYTES` | 256 MiB |
| `MAX_PDF_PACKAGE_EXPANDED_BYTES` | 512 MiB |
| `MAX_PDF_PACKAGE_DOCUMENTS` | 250 |
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
| `PUBLICATION_BUILD_SUMMARY_MAX_ITEMS` | `50000` |
| `SIGNED_MANIFEST_RETENTION_DAYS` | `30` |
| `PUBLICATION_ARTIFACT_ROOT` | `/var/lib/fire-backend/publications` |
| `PUBLICATION_ARTIFACT_TEMP_ROOT` | `/var/lib/fire-backend/publications/.tmp` |
| `PUBLICATION_ARTIFACT_MAX_BYTES` | 600 MiB (per encrypted artifact; document datasets encrypt individual PDFs) |
| `PUBLICATION_ARTIFACT_STALE_SECONDS` | `3600` |
| `PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS` | `2` |
| `PUBLICATION_TERMINAL_SNAPSHOT_RETENTION_DAYS` | `30` |
| `PUBLICATION_RETENTION_BATCH_SIZE` | `100` |
| `PUBLICATION_KEK_VERSION` | `1` |
| `PUBLICATION_SIGNING_KEY_VERSION` | `1` |

The artifact temporary root must be a strict descendant of the artifact root
so final promotion is an atomic same-filesystem rename. Normal source edits
coalesce for the nightly build rather than a sliding debounce.

Retention is lifecycle-safe and bounded. The current publication and the two
newest usable rollback predecessors are protected. Older successful
superseded publications become `OBSOLETE`; aged `FAILED` and `CANCELLED`
attempts retain their terminal status while only their source snapshots are
purged. Do not lower the protected predecessor setting below the approved
operational policy without a separately approved lifecycle change.

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

## Application-secret encryption

Application credentials stored in PostgreSQL use their own AES-256-GCM key
ring. It is unrelated to publication encryption and signing credentials. The
web service receives it through systemd as `application-secret-kek-ring`;
non-systemd deployments set the explicit `APPLICATION_SECRET_KEK_CREDENTIAL_PATH`
to an equivalently protected file. Do not put key material in the environment
file in production.

| Setting | Default |
| --- | --- |
| `APPLICATION_SECRET_KEK_VERSION` | `1` |
| `APPLICATION_SECRET_KEK_CREDENTIAL_PATH` | `$CREDENTIALS_DIRECTORY/application-secret-kek-ring` (explicit development/test override only) |

The credential is root-managed mode `0600` JSON. Its version-to-key format
allows a new active key to be introduced while older encrypted database values
remain decryptable until they are re-encrypted:

```json
{"keys":{"1":"<standard-Base64 raw 32-byte AES key>","2":"<...>"}}
```

Fresh installs and upgrades from releases predating outbound mail provision an
absent initial credential automatically. A valid existing credential is reused
byte-for-byte; a malformed existing credential is never replaced automatically,
because doing so could make stored credentials undecryptable. Restore or repair
that root-managed file from the corresponding key backup before redeploying.

`APPLICATION_SECRET_KEK_VERSION` selects a listed version for new writes. Keep
previous entries until all values using them have been re-encrypted. The
application fails closed if the credential is absent, malformed, the selected
version is unavailable, or authentication fails.

### Application-secret KEK rotation and recovery

Generate each new key as 32 cryptographically random bytes and store its
standard-Base64 value only in the root-managed key-ring credential. Back up that
credential separately from PostgreSQL, with access controls equivalent to other
production key material. A PostgreSQL backup alone cannot restore encrypted mail
credentials: recovery requires every corresponding application-secret key
version.

To rotate an outbound-mail credential key, first add the new version to the
key-ring while retaining all old versions, then set
`APPLICATION_SECRET_KEK_VERSION` to the new version and reload the service.
Run `python manage.py rotate_outbound_mail_secrets --status`, then
`python manage.py rotate_outbound_mail_secrets --rotate`; rerunning the rotate
command is safe after an interruption because already-current envelopes are
skipped. Finally run `python manage.py rotate_outbound_mail_secrets
--check-retirement OLD_VERSION`. Remove an old live key only after that check
succeeds.

Keep prior key versions for as long as any retained database backup might need
them. Even after all live rows have been re-encrypted, historical backups can
contain ciphertext under retired versions. If rotation fails, restore the
previous active-version deployment setting while retaining the complete key
ring, remediate the sanitized command reference, and rerun; never remove a key
needed by a still-live row or retained backup.

## Outbound-mail egress

API mail providers use direct HTTPS egress by default. Set
`OUTBOUND_MAIL_HTTPS_PROXY` only when the deployment requires a dedicated HTTPS
egress proxy; it accepts an `http://` or `https://` proxy URL. When configured,
outbound-mail requests use that proxy exclusively and do not retry directly.
This deployment setting is not stored in PostgreSQL or configurable by an
administrator.

| Setting | Default |
| --- | --- |
| `OUTBOUND_MAIL_HTTPS_PROXY` | unset (direct egress) |
| `OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS` | `5` |
| `OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS` | `15` |
| `OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS` | `15` |
| `OUTBOUND_MAIL_SMTP_ALLOWED_HOSTS` | unset |
| `OUTBOUND_MAIL_SMTP_ALLOWED_NETWORKS` | unset |

SMTP destinations must be DNS hostnames that resolve only to globally routable
addresses. IP literals and private, loopback, link-local, reserved, multicast,
or documentation addresses are rejected for both delivery and verification.
For a self-hosted relay, deployment operators may set a comma-separated exact
hostname in `OUTBOUND_MAIL_SMTP_ALLOWED_HOSTS` or a narrowly scoped CIDR in
`OUTBOUND_MAIL_SMTP_ALLOWED_NETWORKS`. These exceptions are deployment-only;
they are not Department or System Admin settings. SMTP connects are pinned to
the checked DNS answers to prevent a later resolver result from changing the
destination.

## Tablets, backups, and restore

Tablet lease duration belongs to each department, not an environment variable:
the default is seven days and the minimum is three days. Automatic check-in
renewal occurs only with 48 hours or less remaining; authenticated tablet
refresh explicitly tops up an eligible active installation.

Backup and restore location, scheduling, and role credentials are deployment
concerns. See [deployment.md](deployment.md) and the operational procedures in
[operations.md](operations.md).

## Report Transfer

These application defaults come from `config/settings/base.py`; integer limits
are checked in `apps/report_transfer/apps.py` and `security.py`. MiB means
1,048,576 bytes. The canonical [feature guide](report-transfer.md) describes the
format/lifecycle; these settings do not enable direct PDF-mail.

| Setting | Application default | Allowed bound / handling |
| --- | --- | --- |
| `REPORT_TRANSFER_KEYRING_PATH` | `$CREDENTIALS_DIRECTORY/report-transfer-keyring` when credentials are available | Explicit development/test override; outside systemd resolves to `/run/credentials/report-transfer-keyring`, with no embedded key fallback |
| `REPORT_TRANSFER_ACTIVE_KEY` | `1` | Must exist in dedicated keyring; identifier matches `[A-Za-z0-9._-]{1,64}` |
| `REPORT_TRANSFER_ROOT` | `/var/spool/firedash-reports` | Private spool; cannot overlap publication, media, import staging or accepted-source storage |
| `REPORT_TRANSFER_MAX_REQUEST_BYTES` | 96 MiB (100663296) | Integer 1 to 128 MiB; whole encoded request plus separate canonical envelope check |
| `REPORT_TRANSFER_MAX_JSON_BYTES` | 1 MiB (1048576) | Integer 1 to 1 MiB decoded report; canonical document independently capped at 1 MiB |
| `REPORT_TRANSFER_MAX_JPEG_BYTES` | 4 MiB (4194304) | Integer 1 to 8 MiB per decoded JPEG |
| `REPORT_TRANSFER_MAX_ATTACHMENTS` | 12 | Integer 0 to 32 |
| `REPORT_TRANSFER_MAX_IMAGE_PIXELS` | 16000000 | Integer 1 to 32000000 per JPEG |
| `REPORT_TRANSFER_RETENTION_HOURS` | 168 | Integer 1 to 168; seven-day hard ceiling and fixed 48-hour post-download ceiling |
| `REPORT_TRANSFER_MAILS_PER_MINUTE` | 3 | Integer 1 to 10 per installation/recipient, rolling minute |
| `REPORT_TRANSFER_MAILS_PER_HOUR` | 10 | Integer 1 to 60 per installation, rolling hour |
| `REPORT_TRANSFER_MAX_REPORTS` | 20 | Runtime clamps effective selection limit to 1 to 100; no silent truncation |

The installer explicitly persists `REPORT_TRANSFER_ACTIVE_KEY` and canonical
`REPORT_TRANSFER_ROOT` in the runtime environment. It preserves the active
version and every existing valid key; it does not persist each size/rate/retention
default as a separate installer choice. Those values come from application
defaults unless explicitly configured. Standard convergence rejects custom spool
paths/direct keyring overrides until reconciled; see
[deployment](deployment.md#automatic-report-transfer-infrastructure-phase-36).

The durable root-owned `0600` credential is
`/etc/fire-backend/credentials/report-transfer-keyring.json`, delivered to web as
`report-transfer-keyring` with systemd `LoadCredential`. Its format is
`{"keys":{"VERSION":"PKCS8_PRIVATE_PEM_STRING"}}`; keys must be RSA, at least
3072 bits, with no duplicate JSON members. No private material belongs in env
files, logs or source. This keyring is separate from publication HPKE/KEK/signing
and application-secret encryption. Add new immutable versions for rotation;
retain historical private keys while any live report requires decryption. Exact
validated replay needs the stored digest, but exports need the private key.

Missing/malformed credentials advertise only `{"version":1,"enabled":false}`.
The standard deployment verifier treats disabled/insecure configuration as failure.
`FIREDASH_PUBLIC_ORIGIN` must be a valid HTTPS origin for link issuance; mail
provider/domain verification remains required. Portal sessions require database
Django sessions; link validity (60 minutes), inactivity (30 minutes), download
interval (48 hours) and transfer-request parser limit (4096 bytes) are fixed
behavior, not additional `REPORT_TRANSFER_*` environment settings. Request
limits still require upstream timeout/concurrency and spool-capacity planning.
