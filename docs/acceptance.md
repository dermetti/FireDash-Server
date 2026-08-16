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
| Tablet lifecycle | Exercise adoption, ordinary lease threshold behavior, Refresh tablet, stale transition, reactivation, revoke/remove. |
| Manifest/download | Verify ETag/304, protected range transfer, cryptographic ETag preservation, signature, HPKE unwrap, and AES-GCM verification. |
| Authorisation isolation | Confirm cross-department/station access is denied for portal and tablet paths. |
| Backup/restore | Verify a backup and complete an isolated restore drill. |
| Swift interoperability | Perform the required iOS/Swift HPKE, signature, manifest, and artifact interoperability test. |

Automated tests and the deployment verifier support these checks; they do not
replace environment-specific operator evidence. Record results in the release
process rather than retroactively marking this checklist complete.

## Fake-iPad beta exercises

Run these only against the intended alpha/LXC environment. The fake client is
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

# Human makes this installation STALE and supplies a reactivation invitation.
$ReactivationToken = "<human-created-reactivation-token>"
python .\tools\fake_ipad.py reactivate --state-dir $State --token $ReactivationToken --simulate-lost-completion-response

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
