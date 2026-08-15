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
   For publication keys, follow the key-rotation procedure and validate new
   artifacts/manifests before normal operation resumes.
4. Review audit and service logs for affected installations, administrative
   actions, and publication work. Revoke/re-adopt installations where the
   installation credential or HPKE key may be compromised.
5. Run the deployment verifier and required acceptance tests after recovery.

Do not treat a repository history rewrite as containment, and do not claim a
formal certification based on these controls.
