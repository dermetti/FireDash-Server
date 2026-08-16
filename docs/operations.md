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

## Publication signing-key rotation

The publication KEK is unrelated to signing-key rotation: **do not rotate the
KEK in this procedure**. Historical Ed25519 public keys must remain in the
root-managed ring while any retained manifest or artifact may reference them.
`GET /api/v1/tablet/signing-keys/<version>` returns only that exact retained
version; an unknown version is 404 and clients must never substitute the
active key.

Use the root-only two-phase helper from the deployed exact-SHA release. It
does not send private material through Django, Gunicorn, HTTP, audit events, or
logs. It stages root-only private material, atomically adds the public key to
the ring, and then atomically swaps the active worker credentials only after
the staged pair and ring entry match.

`prepare` is fail-closed: duplicate versions, malformed/empty rings, invalid
keys, and staged-version conflicts fail before it creates staging material,
replaces the ring, or invokes systemd. A duplicate version is never a reason
to regenerate or overwrite anything. If prepare or activate fails, do not try
to repair it by rewriting historical signatures or publication metadata; use
`status`, the deployment verifier, and the retained root-only backup/staging
material to investigate.

After the ring has been atomically prepared, the helper refreshes the web
service's public `LoadCredential` snapshot. If that refresh fails, it returns
non-zero and does not claim preparation succeeded; the old active signer still
remains active and the operator must restore web-service health and verify the
retained ring before any activation.

```sh
sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key status
sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key prepare --version 2
sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key status
```

Before activation, with the old signer still active, ensure `fire-backend` has
restarted after prepare (the helper attempts this) and use an authorised test
tablet/fake iPad to fetch and validate both the old and prepared public key.
Run the normal exact-SHA installer convergence procedure before and after the
transition as required by the release process; installer reruns preserve every
historical ring entry and never regenerate signing keys. When those checks are
clean, activate:

```sh
sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key activate --version 2
sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key status
# Run the approved exact-SHA installer convergence rerun for this release.
sudo /srv/firedash/current/deploy/verify-deployment.sh
journalctl -u fire-publication-delivery -u fire-publication-build --since '15 minutes ago'
```

The helper pauses only publication build activation (service, socket, and
timer) plus delivery during the active credential transition, then re-enables
their expected state. Maintenance is untouched, and the helper does not run a
build.
Use the normal administrator publication workflow to produce one legitimate
new signed manifest/artifact, then verify both that new v2 material and old v1
material with the exact corresponding public keys. Do not “roll back” by
rewriting prior signatures or publication metadata. Staging remains under
`/etc/fire-backend/credentials/publication-signing-key-staging/`; remove a
root-only staged private key only after the retention/recovery review, never
the retained public-ring entry merely because it is no longer active.

## Backup and restore

Monitor backups under the backup role, verify completion and retention, and
periodically run a restore drill into an isolated environment. A successful
backup command is not proof of recoverability. Never restore over production
as part of an investigation.

## Canonical import staging

Import sources and temporary sanitized PDF outputs live under the private
`INGESTION_STAGING_ROOT`, never under the release tree or an Nginx location.
`fire-publication-maintenance.service` runs `python manage.py
cleanup_import_staging` without publication credentials to expire unapplied
previews after seven days and applied source material after 30 days (or the
configured retention). This cleanup does not change canonical records,
publication artifacts, audit history, or keys.

## Safe troubleshooting

Run `nginx -t` before an Nginx reload and run the deployment verifier after
deployment changes. Check filesystem ownership, systemd credential delivery,
database role privileges, and unit hardening with read-only inspection first.
Do not loosen PostgreSQL HBA, disable audit triggers, expose credential files,
or recursively change ownership or modes to make a command work. Escalate a
key compromise according to [security.md](security.md).
