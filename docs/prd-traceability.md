# PRD Traceability

This is the initial merged traceability baseline for `Product Requirements Document.md` version
3.2. It records repository evidence, not a production certification. “Implemented” means source
or configuration exists in this repository; it does not mean a deployment, systemd execution,
restore drill, external service, or acceptance test has been performed.

| Status | Meaning |
| --- | --- |
| Implemented | Source, migration, test, or deploy artifact is present; normal verification remains required. |
| Environment gate | Requires values, credentials, OS packages, host configuration, or external infrastructure not committed here. |
| Verification gate | Requires an execution or acceptance test; do not infer success from the artifact. |
| Out of scope | Explicitly excluded by the PRD. |

## Product, scope, and platform

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| 1, 3, 6 | Provision reference data only; never accept incident data or generic tablet uploads | Scoped Django apps, tablet API, PRD boundary | Implemented; negative endpoint/security review remains a verification gate. |
| 2, 5 | Django/DRF, PostgreSQL/PostGIS, Gunicorn, Nginx, systemd, open-source stack | `pyproject.toml`, requirements, `deploy/` | Environment gate for package installation and supported production versions. |
| 4 | Debian LXC/private HTTPS deployment, no public exposure | `docs/deployment.md`, `deploy/nginx/fire-backend.conf` | Environment gate: Proxmox, Headscale/private network, firewall, DNS, and certificates are external. |
| 3.2 | No Docker, Redis, Celery, Kubernetes, MinIO, SPA, gateway, billing, public registration, or incident features | Repository architecture and PRD exclusions | Out of scope. |
| 2 | Deny-by-default, tenant/station/role/property authorization and isolation tests | `apps/` authorization services and tests | Implemented; full test suite is a verification gate. |
| 2, 24 | OWASP ASVS/API Top 10 posture and reviewed cryptography | Settings, dependency review, CI configuration | Verification gate: security review and dependency maintenance are ongoing. |

## Foundation and administration

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| Phase 1 | Custom user model, health endpoints, environment settings, tests and CI | `apps/accounts`, `config/`, `.github/workflows/ci.yml` | Implemented; production health check is an environment/verification gate. |
| Phase 1 | Unprivileged Gunicorn via socket and TLS Nginx proxy | `fire-backend.service`, `.socket`, Nginx config | Environment gate: install, socket ownership, certificate paths, `nginx -t`, and HTTPS checks. |
| Phase 2 | MFA, role assignment, scoped admin surfaces, append-only audit framework | `apps/accounts`, `apps/audit`, related tests | Implemented; database grants/triggers must be applied and tested in deployment. |
| Phase 3 | Departments, stations, vehicles, personnel assignments, setup workflows | `apps/portal`, `apps/assignments`, `apps/personnel` | Implemented; beta workflow validation remains a verification gate. |
| Phase 4 | Personnel lifecycle, eligibility, verified email, offboarding, retention/anonymization | `apps/personnel`, `docs/personnel-retention.md` | Implemented; scheduled retention operation is an environment/verification gate. |
| 7 | System administrators cannot access operational data in normal application use | Scoped views/services and role tests | Implemented; use of Django superuser and infrastructure access are operational controls. |

## Reference data and publication

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| Phase 5 | Hydrant import, PDF quarantine, sandboxing, sanitization, validation, accepted storage | `apps/reference_data`, `deploy/scripts/fire-pdf-sanitizer-broker`, `deploy/systemd/fire-pdf-sanitizer@.service` | Implemented; OS sandbox tools, paths, limits, and Nginx upload limit are environment gates. |
| Phase 6 | Scope state, revisions, dirty detection, locked job queue, drafts, approval, rollback | `apps/publications`, worker unit and timer | Implemented; timer execution and database locking behavior in production are verification gates. |
| Phase 6 | Expire temporary personnel assignments | `expire_temporary_assignments` command and timer units | Implemented; timer enablement is an environment gate. |
| Phase 6.1 | Immutable registry codes, scope/schema compatibility, features, generic builders | `apps/publications` registry/builders and tests | Implemented; fourth-type acceptance coverage remains a verification gate. |
| Phase 7 | Credential delivery, KEK/signing keys, CEKs, encryption, HPKE grants, signed manifests | `SignedManifest` request/result migration, publication worker, systemd credentials, crypto code/tests | Web processes coalesce authorization/state-hash requests but never load KEK or signing credentials; the worker revalidates authorization and persists canonical payloads, signatures, algorithm, and signing-key version. Environment gate for key generation, protected credential files, key versions, and interoperability acceptance. |
| 24, 25 | Encryption, protected artifacts, and append-only audit events | publication artifact code, audit app, PostgreSQL setup docs | Environment gate for database permissions/triggers and protected filesystem ownership. |

## Phase 8 adoption and leases

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| Phase 8 | Tablets, installations, invitations, adoption API and HPKE proof | `apps/tablets/models.py`, `services.py`, `api.py`, tests | Implemented; tablet-client interoperability is a verification gate. |
| Phase 8 | Department-owned tablet lease policy; check-in renews only within 48 hours and authenticated Refresh tablet explicitly tops up an active lease | `apps/organizations/models.py`, `apps/tablets/services.py`, `apps/tablets/api.py` | Implemented; live API operation is an environment/verification gate. |
| Phase 8 | Expired active installations become stale with an audit event | `mark_stale_installations`, new management command and `fire-stale-installation.*` units | Implemented; `daemon-reload`, timer enablement, and successful execution are environment/verification gates. |
| Phase 8 | Stale tablets cannot self-reactivate; department admin reactivation rotates credentials | tablet services, views, API and tests | Implemented; administrator beta workflow confirmation is a verification gate. |
| Phase 8 | Removal/revocation denies future access and revokes grants | `remove_tablet` and manifest/key-grant services | Implemented; operational lost-device procedure is documented in `administrator-beta-guide.md`. |

## Phase 9 provisioning and operations

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| Phase 9 | Configuration and manifest APIs, ETags, RFC 9457/OpenAPI | `apps/tablets/api.py`, `config/api.py`, signed-manifest worker flow | Manifest returns only an exact persisted signed result; preparation returns a 202 RFC 9457 `manifest-pending` response with retry guidance. Generated schema and tablet integration remain verification gates. |
| Phase 9 | Authorized UUID-only package download with Nginx internal redirect | publications download code and `deploy/nginx/fire-backend.conf` | Implemented; Nginx install/test and direct-location denial test are environment/verification gates. |
| Phase 9 | Protected range downloads | application/Nginx delivery configuration | Verification gate: confirm range behavior through the deployed proxy. |
| 26 | Nightly PostgreSQL dump, encrypted restic copies, plans/artifacts/keys, retention, off-Proxmox destination | `fire-backup`, `fire-backup.service`, `fire-backup.timer`, deployment/configuration docs | Environment gate: install restic/client tools; create backup role, credentials, repository/provider configuration, and off-Proxmox destination. |
| 26 | Backup-failure logging and log rotation | systemd journal-backed backup service; Nginx/journal guidance | Environment gate: journal retention, logrotate where applicable, monitoring, and alert routing. |
| 26 | Monthly restore tests | `fire-restore`, `fire-restore.service`, `docs/operations.md` | Verification gate: no restore or systemd test is claimed. Restore is isolated and manual by design. |
| 26 | Health checks, automatic restart, time sync | health endpoints and `Restart=on-failure` service | Environment gate: HTTPS monitor and host time synchronization. |
| Phase 9 | Production checklist, security/logging guidance, administrator beta documentation | `docs/deployment.md`, `configuration.md`, `operations.md`, `administrator-beta-guide.md` | Implemented; administrators and operators must follow the procedures. |

## Test and release gates

| PRD area | Requirement | Evidence | Status / gate |
| --- | --- | --- | --- |
| 27 | Authorization, publication, crypto, file-security, download, audit, retention, and end-to-end tests | test modules under `apps/` and `config/` | Verification gate: execute the applicable suite and record results. |
| 27.9 | Ruff, mypy, bandit, pip-audit, pytest, Django checks, migrations, OpenAPI and crypto interoperability | CI workflow and development requirements | Verification gate: CI result and external Swift interoperability evidence are required. |
| 28 | All acceptance criteria, including backups/restores tested | This matrix and referenced artifacts | Verification gate: the PRD’s completion criterion is not satisfied until each environment and test gate is closed. |
