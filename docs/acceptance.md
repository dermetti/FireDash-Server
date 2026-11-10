# Production-like acceptance checklist

This checklist records evidence still required before an environment is called
production-ready. A source implementation is not itself acceptance evidence.

| Area | Required evidence |
| --- | --- |
| Installer convergence | Run the installer twice; confirm units, files, and symlinks converge. |
| HTTPS and health | `nginx -t`, reload through the approved process, and HTTPS `/health/` check. |
| PostgreSQL | Deployment verifier confirms PostGIS, separated roles, constraints, and audit immutability. |
| Credentials | Verifier proves web cannot read KEK/private signing key; delivery/build can, maintenance cannot. |
| PDF boundary | Upload/sanitise an allowed fixture and confirm broker/sandbox limits and private paths. |
| Publication pipeline | Source edits coalesce; nightly 00:05 build auto-publishes a current result; failure preserves known-good data. |
| Manual publication | Build & publish now and bulk expedite wake the socket after commit without web systemd privilege. |
| Delivery latency | Request a changed manifest and confirm delivery service handles `202`/ready transition inside client retry expectations. |
| Maintenance | Verify manifest retention and stale artifact cleanup without private publication credentials. |
| Tablet lifecycle | Exercise adoption, ordinary lease threshold behavior, inactive empty-manifest sync, stale auto-recovery, revoke/lost/retire. |
| Manifest/download | Verify ETag/304, protected range transfer, cryptographic ETag preservation, signature, HPKE unwrap, and AES-GCM verification. |
| Authorisation isolation | Confirm cross-department/station access is denied for portal and tablet paths. |
| Role-based administration | As Department, Station, and System Administrator, verify only valid navigation/destinations are available; confirm Overview is attention-first, the top-bar count matches distinct actionable problems, and an empty attention state is quiet and positive. |
| Administrator lifecycle | Suspend, reinstate, and revoke both administrator scopes; prove suspended/revoked accounts lose access and the last effective Department Administrator cannot be removed. |
| Imports and canonical publication state | Exercise Station/Vehicle and Personnel import review, ambiguous/missing station rejection, atomic failure, and one dirty/publication scope per affected canonical scope. |
| Vehicle retirement recovery | Retire a Vehicle with an assigned Tablet; confirm explicit unassigned attention, preserved Tablet/install history, revoked former distribution, and supported reassignment to an active same-department Vehicle. |
| Publication recovery | Verify rollback, active deletion with a safe predecessor, rejection without one or during conflicting live work, retention protection under the configured READY-rank or strict policy, terminal snapshot purge, and post-commit artifact cleanup. |
| Backup/restore | Verify a backup and complete an isolated restore drill. |
| Swift interoperability | Perform the required iOS/Swift HPKE, signature, manifest, and artifact interoperability test. |

Automated tests and the deployment verifier support these checks; they do not
replace environment-specific operator evidence. Record results in the release
process rather than retroactively marking this checklist complete.

## Beta exact-SHA gate

Create and push the final candidate commit, deploy that exact SHA with the
release entrypoint, and record the SHA alongside the verifier output. Do not
perform beta acceptance from an uncommitted tree. Complete the role-based
administration checks above for a Department Administrator, Station
Administrator, and System Administrator, then complete the Tablet lifecycle
exercise (including stale recovery and terminal-state denial) with a real or
fake-iPad client as appropriate.

## Fake-iPad beta exercises

Run these only against the intended authorized beta/LXC environment. The fake client is
an independent verifier: it never changes compatibility policy, tablet state,
invitations, or signing keys. In Windows PowerShell, set the server and the
human-created invitation token(s), then use a dedicated local state directory:

```powershell
$Server = "https://firedash.example.org"
$AdoptionToken = "<human-created-adoption-token>"
$State = ".firedash-fake-ipad-beta"
python .\tools\fake_ipad.py adopt --server $Server --token $AdoptionToken --state-dir $State --app-version 1.0.0 --app-build 10
python .\tools\fake_ipad.py verify --state-dir $State
python .\tools\fake_ipad.py check-in --state-dir $State
python .\tools\fake_ipad.py refresh --state-dir $State
python .\tools\fake_ipad.py status --state-dir $State
```

For app-version policy, a system administrator first sets the `/api/v1`
minimum to `1.2.0`. The first command must report 426 with
`client_update_required`; it must not purge the local state. The next command
reports the new version/build before the server evaluates the restriction:

```powershell
python .\tools\fake_ipad.py check-in --state-dir $State --app-version 1.0.0 --app-build 10
python .\tools\fake_ipad.py check-in --state-dir $State --app-version 1.2.0 --app-build 25
python .\tools\fake_ipad.py check-in --state-dir $State --telemetry version-only
python .\tools\fake_ipad.py check-in --state-dir $State --telemetry none
```

The system administrator must reset the policy to unrestricted after this
exercise. Completion-recovery and terminal-state exercises require separate
human-created invitations/state changes; the client does not manufacture them:

```powershell
$RecoveryToken = "<human-created-adoption-token>"
python .\tools\fake_ipad.py adopt --server $Server --token $RecoveryToken --state-dir ".firedash-fake-ipad-lost-adoption" --app-version 1.2.0 --app-build 25 --simulate-lost-completion-response

# Human replaces or revokes this installation first; status must be allowed and all probes denied.
python .\tools\fake_ipad.py terminal-matrix --state-dir $State
```

For ordinary unchanged state and a complete encrypted artifact path:

```powershell
python .\tools\fake_ipad.py update-check --state-dir $State --expect-unchanged station_personnel
python .\tools\fake_ipad.py download --state-dir $State
```

The deployed public-key ring retains historical keys. During a human key
rotation, use the root-only two-phase helper and the fake client verifies both
exact versions without substituting the active key:

```powershell
python .\tools\fake_ipad.py signing-key 1 --state-dir $State
python .\tools\fake_ipad.py verify --state-dir $State
# On the LXC as root: prepare v2, verify both keys while v1 stays active,
# then activate v2. Do not rotate the publication KEK.
# sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key prepare --version 2
# sudo bash /srv/firedash/current/deploy/rotate-publication-signing-key activate --version 2
python .\tools\fake_ipad.py signing-key 1 --state-dir $State
python .\tools\fake_ipad.py signing-key 2 --state-dir $State
python .\tools\fake_ipad.py verify --state-dir $State
```

The deterministic complete-manifest fixture is checked in at
`apps/publications/tests/fixtures/complete_manifest_contract.json` and is
verified by both the server fixture test and the fake-iPad client test using
the live canonicalisation and Ed25519 verification paths.

## Report Transfer beta acceptance

This is required human/environment evidence, not a completed checklist. Existing
functional tests cover the scenarios linked in the
[feature guide](report-transfer.md#compatibility-and-focused-verification).
Historical test results do not certify this candidate SHA. No tests or operational
acceptance were executed during this documentation consolidation. Record the
candidate SHA, environment, operator, time and outcome for each release check.

- [ ] Real tablet/Swift: independently construct ReportDocument v1 and exact
  RSA-OAEP-256/A256GCM JSON/JPEG JWE; verify limits, scope, hashes, matching IDs,
  201 acceptance, exact 200 retry after a lost response, status and 409 conflict.
  Preserve existing adoption/HPKE/signature/publication interoperability checks.
- [ ] Queue and snapshot: offline finalized packages survive restart; pending or
  unconfirmed reports block access issuance. Confirm READY, request access for
  the app-authenticated IC, and recover a lost transfer response with the saved
  UUID without another send. Later reports enter only a new intentional snapshot.
- [ ] Verified test mailbox: same-department active IC/domain policy accepts;
  foreign/ineligible/unverified recipients fail. One neutral mail arrives without
  report attachments. Scanner GET leaves the token unused; explicit CSRF POST
  redeems once. Reuse/60-minute expiry fails; active sessions honor 30-minute idle.
- [ ] Browser: confirm current compact ordered overview, separate detail action
  and collapsed PDF/JPEG list; detail shows dynamic fields, client/server times
  and gallery. For five synthetic reports with three JPEGs each, inspect every
  PDF and all 15 original JPEG downloads, stable distinct filenames and previews.
  Confirm safe headers/cookies and no token paths in deployed logs/telemetry.
- [ ] Access denial: anonymous/foreign report and attachment UUIDs fail; mailbox,
  policy and explicit installation/tablet/department revocations stop access.
  Ordinary lease expiry/STALE and station reassignment preserve issued grants.
  Used-token two-step revoke stops that generation; replacement invalidates old
  sessions and an old token cannot revoke the replacement. Tablet revoke is safe
  to repeat. Expired members disappear and direct download fails while live
  members remain available.
- [ ] Retention: HTML/previews and interrupted/prefetch requests do not mark a
  download. Complete explicit PDF/JPEG handoff shortens to the earliest 48-hour
  deadline within seven days; retries never extend it. In an isolated fixture,
  verify receipt recovery, failed unlink retry, orphan sweep and effective denial
  before deletion. Record actual five-minute timer execution and purge evidence.
- [ ] Native Debian/LXC: isolated helper tests plus standard exact-SHA installer
  convergence/final verifier prove actual systemd credential mounts, namespace
  probe, private spool modes, denied worker/Nginx access, expiry unit schedule
  and key preservation. Windows/Git Bash fixtures do not prove these boundaries.
  Verify protected credential backup restoration and external snapshot exclusions;
  provider mail delivery is a separate check, not part of deployment verification.

Use an authorized disposable installation and test mailbox for Fake iPad:

```powershell
python -m tools.fake_ipad report-test storm --recipient-personnel-id <test-IC-UUID> --server https://test.example --state-dir C:\private\test-ipad --json
python -m tools.fake_ipad report-test link --recipient-personnel-id <test-IC-UUID> --server https://test.example --state-dir C:\private\test-ipad --json
```

These are state-writing examples, and `link` can send real email; do not run
against production. Use the existing adopted private state root. For lost-response
recovery, `status`, exact `retry`, saved transfer request UUIDs and explicit
`revoke`, see [Fake iPad usage](report-transfer.md#compatibility-and-focused-verification).
Run cleanup acceptance only in the authorized isolated environment; no live
cleanup is part of this documentation task.

## Document Generation v2 acceptance

These checks require human-controlled deployment/live evidence for the exact
candidate, not historical local test counts. Actual applied migration state is
unknown from the repository. Review server/client release order and the
[upgrade inventory and rollback precautions](deployment.md#publication-policy-and-document-schema-upgrades)
before separately authorizing any upgrade or cleanup. Preserve the existing
ACTIVE Fake iPad installation, P-256 key, credentials and verification history;
do not adopt again, reset or delete its private state root for this exercise.

- [ ] In the approved maintenance window, verify forward migration state, generic
  tables/readiness guards, quiesced/restarted workers and the exact-SHA deployment
  verifier. Backups and private pre-removal legacy-artifact inventory are reviewed.
- [ ] Build fresh complete schema-2 Fire Plans, KLGV (when enabled), Department
  Files and Station Files. Verify `document-manifest-v2`, generic generation URLs,
  complete frozen metadata and READY installation-specific grants. Empty complete
  snapshots also activate correctly; no schema-1 document fallback is used.
- [ ] Run `python -m tools.fake_ipad verify --state-dir <existing-private-state-root>`
  from the canonical workspace only after authorizing live verification. Verify
  complete/generation Ed25519 signatures with exact retained key versions, HPKE
  binding/unwrap, AES-KW, ciphertext lengths/hashes, AES-GCM and sanitized PDF
  hashes/membership. Counts come from current signed data; no fixed historical
  Fire Plan count is contractual.
- [ ] Exercise metadata-only reuse, retained ciphertext identity checks, missing
  or tampered bytes, unsupported optional/required datasets and schema handling,
  empty generations, local space failure and atomic activation. Preserve prior
  complete data on failure. Cached/reused snapshots are reported separately from
  new downloads. Document plaintext stays in memory; `--save-plaintext` applies
  only to the tool's JSON/GeoJSON behavior.
- [ ] Verify complete tablet manifest 202 problem JSON versus empty generation
  202 with `Retry-After: 5`; generation manifests have no 304 contract. Ciphertext
  304 reuse requires bytes verified for size/hash during that run. Test current
  scope authorization for retained URLs and denial after revoke, lease expiry,
  station reassignment and cross-tenant identity substitution.
- [ ] Real Swift/tablet independently verifies the same protocol, cache binding,
  complete snapshot activation, original PDF rendering and recovery. Fake iPad
  success is not Swift interoperability or a complete iPad UI acceptance result.

## Publication and Explorer beta acceptance

- [ ] As authorized System/Department admins, inspect shared detail
  status/actions/history, Rebuild confirmation, disabled/conflicting work and
  stale duplicate refusal. Rebuild only one chosen scope; old content stays
  authoritative until success. Check the Station-admin parent publication panel
  without granting access to department Publications. Keep incomplete legacy history non-restorable
  without rewriting or deleting it merely for acceptance.
- [ ] In disposable owner-approved data, exercise complete/empty restore, strict
  rejection of incomplete generations, active deletion with a safe predecessor
  and refusal without one. Review both retention policies with the read-only
  audit; actual flag activation/destructive cleanup requires separate approval.
- [ ] Exercise the same Explorer for normal Department/Station admins and temporary
  station access: search paths, sort/page, upload/replace/delete, folder mutations,
  source quotas and per-file failure feedback. Prove unauthorized direct GET/POST
  and PDF requests fail. Temporary grants expire/release and allow one successful
  delete only. Publication controls remain in their scope-specific destinations.
- [ ] Manually verify keyboard/mobile dialogs, focus return, station combobox
  stale-choice protection, readable errors/labels and no-JavaScript fallbacks.
  Capacity approval queues, warnings and tablet storage gates must be absent.

## Remaining beta preparation

Environment evidence remains required for real Swift/tablet, mailbox/browser,
native Debian/LXC systemd/credential/sandbox boundaries, backup restoration and
production-like acceptance. Record candidate SHA and evidence; do not infer
production success from repository tests or migration files. Complete an
independent security assessment, EULA/privacy/legal review and installer polishing.
The Bash installer currently uses manual TLS and existing logging; automatic
Let's Encrypt and verbose/quiet modes remain planned improvements. These are
remaining preparation tasks, not completed features or new implementation specs.
