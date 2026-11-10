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
[`dependency-audit` CI job](../.github/workflows/ci.yml) and its
`dependency-advisories` artifacts and summary as ongoing evidence. Review the
actual scan status and candidate environment; mandatory CI job success alone does
not establish dependency security. See the
[release advisory review policy](deployment.md#dependency-advisory-review-and-release-approval).

## Report Transfer trust boundary

[Report Transfer](report-transfer.md) is separate from publication/reference-data
ingestion. The authenticated installation uploads flattened JWE using dedicated
RSA-OAEP-256/A256GCM keys. Scope, hashes, ReportDocument structure and actual JPEG
content are validated before ciphertext persistence. The web process decrypts in
memory for validation/rendering; this is not encryption that hides reports from
the authorized server. Report keys never substitute for publication or application
keys, and publication workers receive no report RSA credential. The encrypted
private spool and database metadata remain separate; PDFs/previews are generated
in memory. Claimed author/finalization/signature metadata does not authenticate
a person; the app is responsible for its operator login.

Recipient authority comes from current persisted same-department active personnel,
incident-commander eligibility, verified valid email and exact approved-domain
policy when enabled. Requests cannot supply a mailbox or arbitrary report list.
A mailbox HMAC binds each immutable grant snapshot; mailbox/policy changes deny
access. Transfer/request/submission identities are scoped to their installation.
New generations revoke the previous installation/recipient grant and its sessions.
Issued portal grants survive ordinary lease expiry/reassignment, but explicit
installation/tablet/department revocation and recipient-policy failures still deny.

Magic Links contain 256 random bits, store only token hashes, expire after 60
minutes and allow one CSRF-protected login. GET is scanner-safe confirmation.
Session and CSRF identities rotate; database sessions have 30-minute inactivity
limits and grant-bound session hashes, with no administrative authority. Used
tokens can revoke only their own generation through explicit two-step POST
within their original lifetime. Each page/preview/download checks current grant,
session, recipient, historical encrypted scope, membership and expiry. UUID
knowledge grants no access. Streams recheck authorization before each chunk;
revocation cannot recall bytes already handed to the transport.

Portal responses are private/no-store with `Referrer-Policy: strict-origin`, a
local-only CSP, frame denial, nosniff and noindex. Secure/SameSite=Lax cookies
and HttpOnly session cookies also apply locally; preserve HTTPS/CSRF/Origin checks.
Templates escape client text. Original JPEG downloads retain original bytes and
may retain metadata; preview thumbnails strip EXIF. Token-path redaction and
proxy logging suppression must extend to ingress/WAF/APM/tracing and mail systems;
never log URLs, bodies, mail contents, token/session hashes or decrypted locals.
Audit metadata contains only necessary safe IDs, times, file types and fixed
outcomes, not personal mailbox/content data. Unknown-token login audit is bounded
globally per minute; known-generation failures are recorded at most once.

Content lives at most seven days from server acceptance, shortened to at most
48 hours after the earliest complete explicit PDF/JPEG server handoff. Previews
and interrupted streams do not count. Receipts preserve observed completions for
retry; no browser-save guarantee is claimed. Scheduled cleanup removes ciphertext
and orphans while retaining idempotency metadata. Keep spool/receipts out of
backups and snapshots, preserve protected key backups and old versions needed by
live reports, and review process dumps/swap/telemetry. Metadata deletion horizons
remain an explicit privacy decision. See the [operational recovery boundaries](operations.md#report-transfer-expiry-and-recovery).
