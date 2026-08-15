# Operations runbook

Use this guide after deployment. Installation and account creation belong in
[deployment.md](deployment.md); settings are in [configuration.md](configuration.md).

## Health and logs

Check the health endpoint through the deployed proxy and inspect only the
relevant service journal, for example:

```sh
systemctl status fire-backend fire-publication-delivery
journalctl -u fire-backend -u fire-publication-delivery --since '30 minutes ago'
curl --fail https://<host>/health/
```

Do not paste credentials, bearer tokens, CEKs, KEKs, private keys, adoption
tokens, or complete signed payloads into tickets or journals.

## Schedulers and workers

Confirm these units have the intended state:

- `fire-publication-delivery.service` — persistent low-latency grants and manifests;
- `fire-publication-build.socket` and `fire-publication-build.timer` — manual wake and nightly 00:05 builds;
- `fire-publication-maintenance.timer` — artifact and manifest housekeeping;
- `fire-stale-installation.timer` — expiration/stale processing;
- `fire-temporary-assignment-expiry.timer` — assignment expiry;
- `fire-backup.timer` — backup schedule.

Use `systemctl list-timers` and targeted `journalctl` queries. A manifest
pending response is normal while the delivery worker creates a signature; if
latency is unexpected, inspect delivery work before touching publication
records. Build failures leave the previous known-good publication current;
inspect the safe recorded build error and use the normal admin workflow to
expedite a corrected source state.

The build socket is advisory. A failed wake does not invalidate committed
database work; the nightly timer remains the fallback. Never use `sudo
systemctl` from the web account or create a helper that grants that privilege.

## Backup and restore

Monitor backups under the backup role, verify completion and retention, and
periodically run a restore drill into an isolated environment. A successful
backup command is not proof of recoverability. Never restore over production
as part of an investigation.

## Safe troubleshooting

Run `nginx -t` before an Nginx reload and run the deployment verifier after
deployment changes. Check filesystem ownership, systemd credential delivery,
database role privileges, and unit hardening with read-only inspection first.
Do not loosen PostgreSQL HBA, disable audit triggers, expose credential files,
or recursively change ownership or modes to make a command work. Escalate a
key compromise according to [security.md](security.md).
