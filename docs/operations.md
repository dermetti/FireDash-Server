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
- `fire-import-staging-maintenance.timer` — expiration of private import staging;
- `fire-stale-installation.timer` — expiration/stale processing;
- `fire-temporary-assignment-expiry.timer` — assignment expiry;
- `fire-report-expiry.timer` - five-minute report ciphertext expiry and receipt recovery;
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
`fire-import-staging-maintenance.service` runs `python manage.py
cleanup_import_staging` as `fire_backend` to expire unapplied previews after
seven days and applied source material after 30 days (or the configured
retention). This cleanup does not change canonical records, publication
artifacts, audit history, or keys. `fire_publication` has no access to this
directory.

## Safe troubleshooting

Run `nginx -t` before an Nginx reload and run the deployment verifier after
deployment changes. Check filesystem ownership, systemd credential delivery,
database role privileges, and unit hardening with read-only inspection first.
Do not loosen PostgreSQL HBA, disable audit triggers, expose credential files,
or recursively change ownership or modes to make a command work. Escalate a
key compromise according to [security.md](security.md).

## PostgreSQL encoding repair

`fire_backend` must be `UTF8` with `LC_COLLATE` and `LC_CTYPE` both `C.utf8`.
The installer creates a new database from `template0` with exactly those values,
independent of `LANG` or other process locale settings. It refuses an existing
database with a different encoding or locale; it never changes, drops, or
reinitializes that database in place.

For an existing incompatible database, schedule a separately approved
maintenance migration. Do not use the normal installer as a repair tool:

1. Quiesce application writers using the normal deployment maintenance
   procedure and take a retained logical custom-format UTF-8 backup with the
   backup role:

   ```sh
   pg_dump --format=custom --encoding=UTF8 --file=<root-only-dump> fire_backend
   ```

   Keep the dump root-only and inspect its command diagnostics; this is not a
   request to repair non-UTF-8 bytes.
2. Create a distinct replacement database owned by `database_owner` with
   `TEMPLATE template0`, `ENCODING UTF8`, `LC_COLLATE C.utf8`, and `LC_CTYPE
   C.utf8`. Apply the FireDash database `CONNECT`/schema grants to that new
   database; do not use `--create` or `--clean` during this repair.
3. Restore the custom dump directly into that replacement:

   ```sh
   pg_restore --exit-on-error --dbname=<replacement-database> <root-only-dump>
   ```

   The restore must stop visibly on an invalid UTF-8 byte sequence; do not
   transliterate, strip, or otherwise repair stored bytes.
4. Before any cutover, verify Django migrations/checks, role access,
   audit immutability, extensions, canonical/publication/ingestion state, and a
   representative Unicode round trip. Only then use an approved maintenance
   cutover procedure to swap database names while application connections are
   stopped; retain the old database under a retired name until acceptance.

Keep the old database and logical dump until the replacement is accepted. Do
not rewrite audit/history rows, publication metadata, or historical signatures
as part of this repair.

## Report Transfer expiry and recovery

`fire-report-expiry.timer` is persistent, starts about five minutes after boot
and repeats five minutes after the service last activated. The oneshot runs
`expire_report_submissions` as `fire_backend` with production settings/runtime
database access, a 120-second timeout and only the report spool writable under
`ProtectSystem=strict`. It has no report RSA credential. Web and cleanup must use
the same canonical `/var/spool/firedash-reports`. Access/status enforce expiry
immediately, even before the next timer run. See [retention](report-transfer.md#retention-and-cleanup).

Start troubleshooting with read-only metadata checks:

```sh
systemctl status fire-report-expiry.timer fire-report-expiry.service
systemctl list-timers fire-report-expiry.timer --all
journalctl -u fire-report-expiry.service -u fire-backend --since '30 minutes ago'
stat -c '%U:%G:%a %n' /var/spool/firedash-reports
```

Cleanup prints `expired`, `orphans` and `failed` counts; deletion failures return
nonzero and preserve references for retry. Inspect disk capacity, directory access,
database health and the timer's last result before an authorized rerun through
the canonical service. Missing ciphertext is tolerated during purge. Durable
completion receipts are reconciled before expiry and preserve the earliest
48-hour deadline even when the original database update failed. Do not delete
receipts, edit expiry timestamps or manually mark downloads successful to clear
an alert. Preserve files on failed deletion; the command is retryable.

The report spool must remain a real private `fire_backend:fire_backend` 0700
directory with 0600 files, outside media/publication/import/accepted-source roots.
Nginx and publication workers must have no direct read access. Do not serve it
as a static alias or recursively change modes/ownership. Monitor capacity and
cleanup failures without dumping JWE, decrypted JSON/JPEG, keys or receipts.
Use safe UUIDs/timestamps/fixed audit outcome codes for diagnosis. Relevant audit
actions use `report_transfer.`: acceptance, grant creation/link outcome, bounded
login failures, login success, revocation, PDF generation, completed download and
content purge. PDF generation alone does not prove download completion.

A FAILED/UNKNOWN/PROCESSING send must not be replayed by a worker or manually
resent from stored internals. An authorized tablet can recover the same request
outcome, then intentionally request a fresh generation. A changed mailbox/domain
policy or explicit security revocation can explain denial; ordinary lease expiry
or reassignment alone does not invalidate an already issued portal grant.

For deployment failures use the existing
[read-only troubleshooting and redeployment procedure](deployment.md#read-only-troubleshooting).
Restore missing/invalid report credentials from protected matching backups;
never regenerate to bypass the initialized marker or historical report records.
Keep every required key version. An interrupted installer may leave writers
stopped: reconcile the recorded failure and rerun the same approved exact SHA.

Normal file backups exclude spool/receipts but include protected credential
backups; external snapshot operators must enforce the same exclusion. A database
restore cannot recover excluded report contents, recreate used tokens or prove
missing download events. Do not fabricate packages or extend deadlines. Revoke
unusable grants and let retained metadata/content follow their existing expiry;
new reports require the normal tablet workflow. Metadata tombstone/transfer/session
retention still needs an explicit privacy policy; schedule `clearsessions`
separately from report cleanup. Review swap/core dumps/APM and token-path logging
at every ingress. Native filesystem, systemd credentials and real timer execution
require [target acceptance](acceptance.md#report-transfer-beta-acceptance).

## Publication retention policy and audit

The application default is `PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=False`:
protect the newest two superseded READY-metadata rows per scope, even if not
strictly restorable. `True` selects up to the configured number of newest strictly
restorable predecessors; invalid generations do not consume slots. Both policies
protect the current publication and serialize against live lifecycle work. The
repository does not establish the deployed flag value.

Owner-controlled read-only inventory in the normal deployed environment:

```console
python manage.py audit_publication_retention
python manage.py audit_publication_retention --json
```

This independent command performs SELECTs without lifecycle write locks or audit
writes; it does not invoke maintenance, cleanup or workers. It compares READY-rank
and strict selection, reports candidate reasons/newly unprotected versions,
potential retirements and active-job/current/attempt protection. Output includes
the flag and predecessor limit; the text's CURRENT/PROPOSED labels name the two
policies even when strict mode is enabled. Save output privately. It excludes
payloads, tokens and wrapping material. It reads no artifact files or private keys;
restore eligibility is metadata-level, not proof of physical integrity or a new
cryptographic signature verification. Repeat during quiet operation if concurrent
changes invalidate the inventory. Long invalid histories can require many bounded
inspections, including the locked recheck; assess maintenance duration from the
inventory before activation rather than imposing a history cutoff.

Before enabling strict selection, separately review every affected scope and
candidate, shared references, storage ownership and recoverable backups; obtain
separate owner approval. The next scheduled maintenance pass after activation can
retire previously protected invalid generations. To withdraw approval before that
pass, prevent maintenance from running, set the flag false through the reviewed
configuration/restart procedure, and re-audit. Once retirement has released
references or deleted ciphertext, toggling false cannot recover it: separately
reviewed backup recovery is required. Never rewrite signed generations in place.

Retention rechecks candidates under scope -> job -> publication locks and skips
current/live work and newly protected predecessors. Retirement preserves database
identity/version/timestamps/signed metadata and audit evidence, marks successful
rows OBSOLETE, revokes delivery grants and releases terminal content/artifact
references. Failed/cancelled snapshots expire after the configured 30-day default
without changing their status. Physical deletion follows commit and final-reference
checks. Generic deletion failures remain in the durable `DocumentArtifactCleanup`
queue; concurrent workers skip locked queue work and retries remain observable.

Maintenance counters distinguish `files_deleted`, `files_already_missing`,
`cleanup_deferred`, `cleanup_failed`, `temporary_entries_removed`,
`document_cleanup_records_processed`, `document_orphan_records_removed` and
`document_cleanup_pending_records`. They count attempts, not unique paths; an
already-missing file is not a failure. Failed deletion remains deferred; inspect
permissions/I/O/path failures without weakening root containment or symlink checks.
Never infer historical untracked ciphertext is disposable from age or delete the
artifact root. Review journal parsers that assume an old combined removal total.

## File publication logical-revert reconciliation

The supported command uses an exact persisted department UUID and the normal
deployed application environment (not test settings):

```console
python manage.py reconcile_file_publications --department <department-uuid>
```

The default is read-only, including no audit writes, though it takes source/scope
locks while assessing. It includes only department/station file scopes in that
department. Review every scope before separately authorizing mutation:

```console
python manage.py reconcile_file_publications --department <department-uuid> --apply
python manage.py reconcile_file_publications --department <department-uuid>
```

Apply rechecks source under department-source -> scope -> library locking,
clears dirty state and cancels redundant ordinary pending jobs/STAGED attempts
that match the active generation. Their version IDs/snapshots and historical
review/audit records remain; source revisions are not reset. Explicit force
rebuilds are preserved. Running jobs are deferred: let the normal worker finish
or recover, then repeat. Changed/incomplete-history scopes stay under normal
publication policy. Rerunning an already reconciled scope is safe. Do not infer
that any production reconciliation or migration has occurred from source code.

## Historical document ciphertext and recovery

Before schema-removal upgrades, follow the private database/artifact inventory
and backup prerequisites in
[Deployment](deployment.md#publication-policy-and-document-schema-upgrades).
Migration presence does not establish any host's applied state. Historical files
without generic database records are deliberately not inferred disposable.
A crash between file promotion and compensation can also leave untracked bytes.
Ordinary generic cleanup handles known orphan records, durable queue entries and
its own failed-build allocations, not an arbitrary historical directory sweep.

For separately owner-authorized physical cleanup, compare the pre-migration
inventory with current generic artifact/reference/cleanup records. Review the
exact proposed path list; every generated path must remain canonical under the
private artifact root and have no live generic artifact/reference. Preserve
current ciphertext and all pending durable cleanup work. Never glob-delete the
shared `documents/{artifact_id}/artifact.bin` tree or infer ownership from its
layout or age. Unknown ownership requires investigation. Recovery of retired or
legacy payloads requires a reviewed compatible database/storage/key backup;
signed manifests and immutable historical rows must never be rewritten to fake
readiness.
