# Installer B.2: protected TLS backup and recovery

This procedure does not authorize live deployment. A native Debian 13 restore
drill remains required; portable mocks are not proof of recoverability.

## Mandatory installer backup gates

Before running a managed-TLS installation or explicit DNS adoption, provision
the dedicated initialized HTTPS Restic repository, its protected access/encryption
files, reviewed plugin provenance and dependency lock described below. Restic and
the selected Certbot environment must already be available. These are operator
prerequisites; the installer never initializes a repository or creates its secrets.

The installer checks repository configuration, reviewed installed dependencies and
authenticated `restic cat config` access before TLS preparation. Before application
quiescence, it requires an actual encrypted snapshot of the candidate TLS state,
including `install.conf.pending`. Missing configuration, unavailable access or an
initial upload failure aborts without stopping application services or promoting
the candidate configuration. Existing DNS-Netcup adoption retains certificate paths,
keys, accounts and authenticator; there are no public-IP or inbound-port checks.

The candidate snapshot is recovery evidence, not permission to promote its pending
configuration. Isolated restore validation understands its recorded configuration
path; review that candidate explicitly before replacement-host activation. Later
snapshots cover ordinary `install.conf`, activation metadata and final release
history. Deployment success requires those snapshots and coverage verification.
A failed final backup/verification restores the previous release-success marker;
rerun the installer to converge. An outage after quiescence still follows the
existing installer maintenance recovery procedure.

Installer child commands explicitly inherit descriptor 9 through
`--installer-lock-fd=9`. The helper verifies the descriptor's root-owned lock inode,
that independent opens are excluded and that the inherited descriptor owns the
exclusive flock. Verification reads coverage without releasing/reacquiring that
lock. Standalone commands still acquire their own exclusive lock. There is no
environment-variable bypass, and concurrent scheduled backup, restore or renewal
cannot enter the critical section. Do not manually supply the internal flag.

Non-secret root:root installer metadata retains its existing 0640 permission
contract (0600 also accepted). Private keys, account secrets, DNS credentials,
repository secrets and recovery journals retain strict 0600 checks.

## Durable DNS rollback

Before modifying an adopted lineage, the manager exclusively publishes and fsyncs
a root-only version-2 journal with the selected lineage, four original archive
links, certificate identity and `pending` state. An existing journal cannot be
overwritten. Successful renewal removes it only after validating the entire live
generation, certificate/key, hostname, validity and deployment marker.

After interruption or failure, recovery validates **every** old archive target
before touching links. It durably records `rollback`, restores each link atomically
and fsyncs the live directory after each update. It validates the complete restored
pair, tests Nginx, reloads Nginx, writes the marker, then removes/fsyncs the journal.
Recovery always reloads: an interrupted reload may have reached workers before
the previous command returned. It waits for a reload that might join an in-flight
job, then requests a distinct reload of the restored files. A partial pair is
never accepted as recovery.

Missing archives, unsafe journals, invalid pairs, expired rollback certificates or
failed reloads retain the journal and fail closed. No archive, key, account or
renewal INI is deleted or rewritten. An expired original can be snapshotted for a
successful recovery renewal, but cannot be activated as a failed renewal's rollback.

Stop the renewal timer while investigating a persistent error. Preserve the
journal and archives. Correct private filesystem/Nginx problems or restore missing
archives into isolation first. After review, retry:

```sh
sudo /usr/bin/python3 /usr/local/lib/fire-backend/tls.py recover
```

This takes the shared installer lock. Repeated completed recovery is a no-op;
renewal and dry-run resolve an existing journal before proceeding. Never delete a
journal to bypass an error. Pre-B.2 unversioned journals are retained and rejected
for explicit private review, rather than silently converted. If the old certificate
has expired, automatic rollback cannot establish safe service: use a separately
reviewed isolated recovery plan.

## Backup ownership and repository

`fire-tls-backup.service` runs as root:root with 0077 umask, a strict filesystem
sandbox, read-only home access and the shared nonblocking installer/renewal lock.
Only its root-only state directory and lock directory are writable. Read-only home
access supports existing protected DNS credentials under `/root`.
`fire-tls-backup.timer` runs daily at 03:15 plus up to 45 minutes random delay,
with persistent catch-up. Successful or failed scheduled renewal also triggers
this service, including retained recovery journals when present. Backup
failure never invalidates an already validated certificate.

The application backup, database role, repository, snapshot history and retention
remain unchanged. TLS uses a **separate encrypted Restic repository and restricted
identity** so the application's existing pruning domain cannot delete TLS history.
The helper rejects equal repository settings; operators must also ensure different
URLs are not aliases for the same underlying repository.
Only HTTPS REST transport is supported. No SSH/shell transport, password commands,
ambient cloud credentials or arbitrary Restic environment options are accepted.
The REST password enters only the root Restic child's private environment; no raw
secret enters command arguments or diagnostic output. Restrict that identity
server-side to the TLS repository, using append-only backup access where supported.

Install Restic from reviewed Debian 13 distribution packages. An authorized
operator initializes the new repository separately. FireDash never initializes,
forgets, deletes or prunes it. No existing repository is contacted during development.
Keep encryption and repository access secrets in an independent protected operator
store: they are excluded from their own encrypted snapshot.

Create `/etc/fire-backend/tls-backup` as root:root 0700. Provision root:root 0600
`/etc/fire-backend/tls-backup.conf` with literal values, without shell quoting:

```ini
RESTIC_REPOSITORY=rest:https://backup.example.invalid/firedash-tls
RESTIC_PASSWORD_FILE=/etc/fire-backend/tls-backup/repository-password
RESTIC_REST_USERNAME=firedash-tls
RESTIC_REST_PASSWORD_FILE=/etc/fire-backend/tls-backup/rest-password
```

Supply both root:root 0600 secret files from the approved secret store. Create
root:root 0600 `tls-backup/plugin-provenance.json` with these fields:

- `packages`: reviewed package-name/version mapping, including `certbot`, `acme`
  and the selected DNS plugin; retain the other restoration dependencies too.
- `plugin_package`: the approved distribution name, empty for HTTP-01.
- `source`: reviewed release SHA, artifact origin and approval reference.

For dns-multi the reviewed versions remain Certbot 5.7.0/dns-multi 5.6.0. Copy the
reviewed hash lock to root:root 0600 `tls-backup/dependency-lock.txt`. For an adopted
native plugin retain its reviewed reproducible dependency inventory and artifact
provenance there. Keep an independent copy of these non-secret approval records.
The helper compares installed metadata through the protected Certbot entrypoint's
absolute Python shebang. Nonstandard wrappers require separate review. No restored
program is executed, and established plugins are never automatically installed.

## Protected inventory

One encrypted snapshot includes only:

- `install.conf`, verified TLS generations, deployment markers and recovery
  journals under `/etc/fire-backend/tls`.
- The selected Certbot renewal INI, live directory and archive directory in the
  FireDash namespace or adopted `/etc/letsencrypt` namespace.
- The referenced account ID under the approved ACME server, including registration,
  metadata and private account key.
- The protected credential file referenced by the selected DNS authenticator.
- Plugin provenance, dependency lock and a root-only hash/ownership/mode/link manifest.

It excludes unrelated lineages/accounts, ACME work/log directories, application
storage, database dumps, application keyrings, Report Transfer spools/receipts and
repository access/encryption secrets. Relative symlinks must resolve inside the
selected inventory. No plaintext archive or credential bundle is staged. The
manifest has metadata and hashes, not secret contents, and is encrypted too.

After reviewed unit/helper installation and explicit repository provisioning:

```sh
sudo systemctl enable --now fire-tls-backup.timer
sudo systemctl start fire-tls-backup.service
sudo /usr/bin/python3 /usr/local/lib/fire-backend/tls_backup.py verify
sudo journalctl -u fire-tls-backup.service
sudo systemctl list-timers fire-tls-backup.timer
```

Installer convergence enables the timer only when its explicit configuration
exists. Managed deployment verification rejects missing/invalid coverage, changed
state/repository binding, a snapshot older than 48 hours, missing units or inactive
timer. A success receipt requires a confirmed full snapshot ID. It is local coverage
evidence, not proof remote storage remains available: monitor failed units and
perform periodic repository checks and isolated restores. Lock contention fails
visibly; retry after installer/renewal finishes. Failed uploads preserve the old
receipt; changed or stale coverage still fails verification. Capacity and separately
authorized TLS retention are operator responsibilities; no automatic pruning is added.

## Isolated replacement-host restore

1. Keep the Debian 13 replacement isolated. Leave application/renewal timers and
   Nginx inactive. Install a reviewed FireDash release, Restic and the approved
   pinned Certbot environment. Prepare root-only `install.conf` for the same
   hostname, TLS mode, paths and selectors. The TLS helper needs no database or
   application keys. Review provenance/lock against independent deployment records
   and place those records in the root-only backup state directory. Supply repository
   secrets from their independent recovery store.
2. Create `/var/lib/fire-backend-tls-restore` and a named empty child such as
   `exercise-2026`, both root:root 0700. Select an explicit full 64-character snapshot
   ID from the protected repository's snapshot listing, never implicit `latest`.
3. Run the following as one command, substituting the reviewed ID:

   ```sh
   sudo /usr/bin/python3 /usr/local/lib/fire-backend/tls_backup.py restore FULL_SNAPSHOT_ID /var/lib/fire-backend-tls-restore/exercise-2026
   ```

   Only an exact TLS-tagged snapshot is accepted. Restic verifies restored content.
   Live paths and nonempty destinations are refused. Failure leaves isolated files
   for private inspection, never partial activation.
4. Validation checks inventoried hashes, root ownership, modes, relative links,
   account JSON, saved authenticator/server/hooks, protected DNS credentials,
   certificate/key, hostname, validity and approved installed package versions.
   Retry with `tls_backup.py restore-check DESTINATION` after correcting isolated
   problems. Missing credentials, wrong permissions and inconsistent pairs fail.
   A retained journal still requires recovery review: do not promote unresolved
   state merely because decryption worked. Review the old archive generation and
   repair/validate isolated live links under an explicit plan before promotion.
5. Successful validation activates nothing. It does not prove provider API access,
   ACME authorization, Nginx readiness or unattended renewal. On the disposable host,
   review promotion of **only inventoried paths** to their original absolute paths,
   retaining links and root modes. Never copy the isolation root wholesale or
   overwrite unrelated certificates. Resolve journals before enabling schedulers.
6. Validate Nginx and HTTPS. Demonstrate systemd staging renewal, failure/retry,
   shared locking and encrypted backup/restore with disposable credentials. Confirm
   unchanged application data and cryptographic selectors. Production activation
   needs separate authorization and explicit old/new scheduler ownership. HTTP-01
   requires updated A/AAAA/firewall rules; DNS-01 needs outbound API/ACME access only.

The native disposable Restic round-trip test exercises encryption and relative links
when Linux/Restic are available. It does not replace an actual Debian 13/systemd
restore drill with real root metadata and staging ACME validation.
