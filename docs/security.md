# Security design and incident response

## Trust boundaries

FireDash separates web, publication, database, and PDF-processing duties.
`fire_backend`/Gunicorn authorises requests but does not receive the
publication KEK or Ed25519 private signing key. Hardened
`fire_publication` delivery and build services receive only the credentials
they genuinely need through systemd `LoadCredential`. Maintenance is
credential-free. The web account can connect to the single publication-build
wake socket but has no sudo, systemd-manager, D-Bus, or arbitrary service-start
privilege.

Nginx is the TLS edge and serves authorised publication artifacts only through
an internal protected location after Django issues `X-Accel-Redirect`.
PostgreSQL roles separate schema ownership, runtime access, and backup work.
Audit events are append-only and database protections must remain enabled.

## Publication and tablet cryptography

Publication artifacts use a random AES-256-GCM CEK. The CEK is protected with
the publication KEK and is distributed only as a per-installation HPKE grant.
Artifacts and manifests are authenticated with Ed25519. Artifact signatures
bind the immutable scope-local publication attempt version; do not reuse or
rewrite a version, ciphertext, wrapped CEK, or signature after creation.

The supported HPKE suite is RFC 9180 DHKEM(P-256, HKDF-SHA256), HKDF-SHA256,
and AES-128-GCM. Public keys are uncompressed 65-byte SEC1 points. Canonical
HPKE context information binds the adoption, grant, and manifest operations;
do not substitute an unreviewed HPKE implementation or suite. Python tests
cover the server-side contract. Swift/Python interoperability remains an
external acceptance requirement and is not evidence of a security
certification.

## PDF sanitisation

Uploaded fire-plan PDFs are quarantined, validated, and sanitised through the
local broker and sandbox service boundary before becoming accepted reference
data. Keep the broker socket, resource limits, private directories, and
systemd hardening intact. Do not allow uploads to bypass quarantine or write
directly to accepted storage.

## Secret handling and logging

Never store secrets in source control, environment examples, fixtures, audit
metadata, browser output, or operational tickets. This includes Django secret
keys, database passwords, adoption credentials, bearer credentials, CEKs,
KEKs, HPKE private material, and Ed25519 private keys. Use protected
credential files and least-privilege ownership. Logs may contain safe IDs,
counts, timestamps, and error summaries, never the secret/payload material.

## Suspected credential compromise

1. Contain access: revoke or replace the affected credential and restrict
   access to its credential file before changing source.
2. Preserve evidence: record a restricted incident entry with affected scope,
   time, and containment action; do not paste the secret itself into it.
3. Rotate the appropriate credential and update systemd credential delivery.
   For publication signing keys, use the root-only two-phase
   `rotate-publication-signing-key` runbook: retain historical public entries,
   prepare and verify the replacement public key first, then deliver the
   replacement private key only to publication workers. The web service
   receives the public ring only. Do not rotate the unrelated publication KEK
   as part of this procedure. Validate old and new artifacts/manifests before
   normal operation resumes.
4. Review audit and service logs for affected installations, administrative
   actions, and publication work. Revoke/re-adopt installations where the
   installation credential or HPKE key may be compromised.
5. Run the deployment verifier and required acceptance tests after recovery.

Do not treat a repository history rewrite as containment, and do not claim a
formal certification based on these controls.

## Historical unresolved dependency findings (2026-11-02)

The local installed-environment `pip-audit --local` observation recorded 22
advisory entries across five packages (105 packages scanned, no skips). These
findings remain open pending severity, reachability and remediation review;
unknown severity must not be treated as harmless. The observations do not
establish the current deployed dependency state or provide release approval.

| Observed package | Observed version | Unresolved advisory identifiers |
| --- | --- | --- |
| certifi | 2026.7.22 | PYSEC-2026-4320 |
| filelock | 3.32.2 | GHSA-v4x8-3wr5-3wq8, GHSA-9vhv-jxww-xfm5, GHSA-567q-5qvw-pf92, GHSA-m462-7rxw-wvpg, GHSA-j8f7-rjxc-mr56, GHSA-8mx5-7m5m-r77g, GHSA-69hj-f68j-2879, GHSA-9c4x-gm8v-wgc4, GHSA-rfpc-qcw5-3pc4, GHSA-cwqr-hh33-gh44, GHSA-fq56-3cx3-fjxp, GHSA-6627-m3xw-93mp |
| msgpack | 1.2.1 | GHSA-j586-36cw-2gc2 |
| sqlparse | 0.5.5 | PYSEC-2026-3698, PYSEC-2026-3697, PYSEC-2026-3699, PYSEC-2026-3696, PYSEC-2026-3923 |
| urllib3 | 2.7.0 | PYSEC-2026-4177, PYSEC-2026-4176, PYSEC-2026-4175 |

The resolved base/dev scans in that historical run timed out and were
**INCOMPLETE**; direct-pin scans did not cover transitives. Neither result
clears the installed-environment findings. Use the existing
[`dependency-audit` CI job](../.github/workflows/ci.yml#L237) and its
`dependency-advisories` artifacts and summary as ongoing evidence. Review the
actual scan status and candidate environment; quality-job success alone does
not establish dependency security. See the
[release advisory review policy](deployment.md#dependency-advisory-review-and-release-approval).
