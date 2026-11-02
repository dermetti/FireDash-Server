# Report Transfer deployment

Report Transfer is installed automatically by the standard installer. Do not
install separate drop-ins or example units. Authoritative definitions are:

- `deploy/systemd/fire-backend.service`
- `deploy/systemd/fire-backend.tmpfiles.conf`
- `deploy/systemd/fire-report-expiry.service`
- `deploy/systemd/fire-report-expiry.timer`

See `docs/deployment.md` for preservation, backups, upgrades and diagnostics.
The exact former example web drop-in is retired automatically; mixed or
conflicting overrides fail closed for explicit administrator reconciliation.
