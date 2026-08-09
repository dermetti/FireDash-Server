# Operations

Check service state with `systemctl status fire-backend.service fire-backend.socket` and logs with `journalctl -u fire-backend.service`.

The process must run as `fire_backend`, not root. Verify the socket is owned by `fire_backend:www-data` with mode `0660`. Verify health through Nginx over HTTPS.

Phase 2 account setup URLs contain one-time secrets. Gunicorn access logging is disabled in favor of Nginx logging, and Nginx disables access logging for `/accounts/setup/` so those tokens never enter server logs.

## Scheduled work

Inspect all scheduled work with:

```text
systemctl list-timers 'fire-*'
journalctl -u fire-publication-worker.service -u fire-temporary-assignment-expiry.service -u fire-stale-installation.service -u fire-backup.service
```

The publication worker runs every minute, temporary-assignment expiry runs hourly, and stale
installation processing runs every 15 minutes. A stale processor failure does not extend a lease:
request authorization also rejects an expired active lease. Investigate and correct failed timers
promptly because the processor creates the required stale audit event and updates tablet status.

Backups run nightly at 02:15 local system time. Confirm a recent successful snapshot with
`systemctl status fire-backup.service` and `restic snapshots` as root with the backup environment
loaded. A non-zero backup service result is the backup-failure record in the journal; alert on it
and do not paste its environment or credential paths into tickets.

## Monthly restore drill

1. Select a recent backup snapshot and record its identifier in the change ticket.
2. Create an empty, restricted target below `/var/lib/fire-backend/restore-tests` and configure the root-only `restore.env` described in `configuration.md`.
3. Run `systemctl start fire-restore.service`, then inspect the restored fire plans, artifacts, credentials, and PostgreSQL dump permissions without exposing their contents.
4. Restore the dump into an isolated PostgreSQL database or disposable LXC. Run application migrations/checks against that isolated database and verify the expected data counts and a sample artifact hash.
5. Record the snapshot, date, operator, validation result, and cleanup. Remove the restored data and `restore.env` after the drill.

This repository provides the procedure and service definitions; restore/systemd execution has not
been performed or claimed by this documentation.

## Security and logging

Use `journalctl` for Django, worker, timer, and backup output, and restrict journal access to
operators who need it. Nginx is the HTTP access-log source; `/accounts/setup/` intentionally has
access logging disabled because its URLs contain one-time secrets. Do not add query strings,
authorization headers, adoption or reactivation credentials, HPKE material, database passwords,
restic credentials, or decrypted dataset contents to logs.

Review Nginx error logs, service failures, authentication/audit events, publication failures, and
backup failures during the operating review. Preserve audit-event retention according to policy;
audit events are application records and do not replace protected system logs or backup monitoring.
