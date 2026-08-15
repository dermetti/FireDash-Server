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
