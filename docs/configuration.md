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
```

`DJANGO_SECRET_KEY`, database credentials, signing keys, key-encryption keys, and backup credentials must never be committed or logged. Signing and key-encryption credentials are introduced in Phase 7 through systemd credentials.

Only the local Nginx proxy may appear in `TRUSTED_PROXY_IPS`. Nginx overwrites inbound `X-Forwarded-For`; application code must not trust a header received directly from a client.
