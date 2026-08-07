# Phase 1 Operations

Check service state with `systemctl status fire-backend.service fire-backend.socket` and logs with `journalctl -u fire-backend.service`.

The process must run as `fire_backend`, not root. Verify the socket is owned by `fire_backend:www-data` with mode `0660`. Verify health through Nginx over HTTPS.

Later phases add publication, expiry, retention, and backup systemd timers. They are intentionally absent from Phase 1.
