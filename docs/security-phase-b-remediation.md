# Phase B remediation and local verification

Follow-up: [Phase B.2 remediation](security-phase-b2-remediation.md) implements
FD-SEC-002 and FD-SEC-011. The reproduction results and file list below describe
the historical Phase B state; the probes have since moved to normal security
regressions that assert the vulnerabilities are absent.

Scope: FD-SEC-008, FD-SEC-009 and FD-SEC-010. The local working tree was
authoritative; the review baseline was e230110f0717e605bd4c34896042ef8e888b0ad0.
No deployment, Git mutations, historical-account cleanup, production data, or
production-role changes are part of this remediation.

## Implemented behavior

- FD-SEC-008: request context accepts a single valid Nginx forwarding address
  only when Gunicorn's server-created `gunicorn.socket` is AF_UNIX and its
  actual local socket name equals `TRUSTED_PROXY_UNIX_SOCKET`. Production fixes
  this to `/run/fire-backend/gunicorn.sock`; other settings disable Unix trust.
  Empty REMOTE_ADDR and HTTP headers cannot establish that boundary. Invalid,
  absent and comma-separated Unix forwarding values yield no client IP.
  Trusted TCP peers retain direct-address fallback for invalid forwarding;
  untrusted TCP peers ignore forwarding. Ambiguous TCP lists now also fall back
  to the peer instead of selecting their first element. Account throttling is
  unchanged; HTTP tests verify both account and IP lockouts.
- FD-SEC-009: the existing department administrator helper runs before GET or
  POST processing. Reauthentication and the existing audited mutation service
  remain in place. Negative tests cover foreign, station, system, suspended,
  revoked and roleless administrators/users without disclosure or mutation.
- FD-SEC-010: station grants and reinstatement validate target authority inside
  the transaction. Active SystemRole rows protect both pending and active
  accounts. Active or suspended department authority conflicts with station
  authority, and another active or suspended station assignment conflicts with
  the single-station model. Grants require an existing same-department authority
  history; new accounts use the existing provisioning service. Revoked rows may
  support explicit re-provisioning, with a new row preserving historical
  provenance. Suspended authority requires explicit reinstatement. Station
  provisioning, grants and reinstatement require operational department/station
  state. Department reinstatement rejects conflicting tenant or system roles.
  Permanent removal protects unrelated active **and suspended** authority and
  pending system administrators before account deactivation/anonymization.
  Department locks preserve last-effective-admin checks; target-account locks
  serialize lifecycle changes across departments, including grants with no
  existing active assignment. A concurrent cross-department grant regression
  permits exactly one grant and preserves both revoked historical rows.

## Compatibility and historical state

The Unix path requires Gunicorn's WSGI `gunicorn.socket` extension. Its presence
was confirmed in the installed Gunicorn `http/wsgi.py` source. A different WSGI
server, middleware that removes that environment entry, or a changed socket
path must receive an explicit deployment review; missing evidence fails closed.
This Windows interpreter has no AF_UNIX support, so unit tests supply mocked
socket state. There is no local Nginx executable and native Gunicorn Unix-socket
execution is unavailable here.

No Nginx, Gunicorn, socket ownership or mode changes were needed. Deployment
regressions verify replacement of X-Forwarded-For on every proxy location,
the Unix-only bind, and the existing fire_backend:www-data 0660 socket and
0750 directory settings. Existing multi-address TCP forwarding deployments
must supply one replaced address; proxy chains are not accepted.

One existing portal test previously granted a second active station to the
same account. It now asserts rejection and then verifies the supported
revoke-before-transfer workflow, retaining its grant/revoke assertions.

Before beta, an owner should separately review historical accounts combining
SystemRole with tenant authority, department and station roles, multiple
active/suspended station assignments, or authority in multiple departments.
These operations reject conflicts and do not reconcile them automatically.
Choose the intended authority through a separately reviewed process, retain
history/audit, and preserve each department's last effective administrator.
No historical database inspection was performed for this task.

## Verification-only findings

Run the disposable probes explicitly (they are outside normal test discovery):

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider tools/phase_b_verification.py -q --tb=short
```

All three reproduction probes passed. These assert the observed defects, not
security acceptance, and must be revised when separately approved fixes land.

- FD-SEC-002: a disposable valid setup token appeared in Django log messages
  for both CSRF rejection (403) and a forced rendering exception (500).
  The Nginx setup location disables access logging but inherits error logging;
  unlike the report-access location, it does not discard request-bearing error
  messages. Native Nginx reproduction remains pending. Recommended separate
  correction: protect setup routes in Nginx error logging, including malformed
  paths and upstream failures; redact setup-token paths in Django/Gunicorn
  diagnostics and exception reporting, including request/traceback data.
  Disabling access logging alone is insufficient. No logging fix was applied.
- FD-SEC-011: two independent pending-MFA sessions both loaded the same device
  before verification, synchronized with a barrier. Both accepted the same
  current TOTP and created successful authenticated sessions and login audit
  events. The device's persisted last_t protects sequential reuse but the view
  verifies independently loaded, unlocked instances. Recommended separate
  correction: acquire a transaction-scoped device row lock before loading and
  verifying the code, retaining that lock through replay-state persistence;
  review enrollment, login and reauthentication consistently and test concurrent
  success/failure and rollback behavior. No MFA fix was applied.

## Pending native acceptance checks

Use a disposable Linux host/container with test-only data and credentials,
the rendered repository Nginx configuration, actual Gunicorn sync workers,
the socket unit's ownership/modes and production-equivalent settings. Do not
run these checks on a live deployment.

1. Exercise both ordinary Gunicorn binding and systemd socket activation.
   Confirm `stat /run/fire-backend/gunicorn.sock` reports fire_backend:www-data
   mode 0660, its parent is restricted, www-data can connect and an unrelated
   unprivileged user cannot. Inspect `ss -ltnp` and `ss -lxnp`: Gunicorn must
   expose only the protected Unix socket, never a public TCP listener.
2. In a temporary test-only endpoint behind RequestContextMiddleware, inspect
   REMOTE_ADDR, socket family/local name and client_ip. An HTTPS request through
   Nginx must show empty REMOTE_ADDR, AF_UNIX and the configured socket name;
   client_ip must equal the actual connecting peer (IPv4 and IPv6 separately).
   Repeat with `X-Forwarded-For: 203.0.113.7, 198.51.100.8`,
   `X-Real-IP: 203.0.113.7` and `Forwarded: for=203.0.113.7` supplied by the
   client. client_ip must still equal the actual peer, proving replacement.
   A request without forwarding headers must produce the same peer address.
3. Through Nginx, submit invalid password attempts for distinct disposable
   accounts from one source until AUTH_THROTTLE_MAX_FAILURES is reached.
   The next attempt for a new account from that source must be locked out;
   a second source must remain usable. Repeat one account across different
   sources to verify account-based lockout independently. Check persisted
   throttle subjects and authentication audit IPs without logging passwords.
4. With a test-only www-data socket client, send missing, malformed and
   comma-separated X-Forwarded-For values directly to the Unix socket;
   client_ip must be null. Disable Unix trust or change the allowed path and
   repeat: all forwarding values must be ignored. Remove the temporary endpoint.
5. For FD-SEC-002 only, use disposable setup URLs: valid path with unavailable
   upstream (502), malformed setup paths (404), oversized request and invalid
   request syntax. Capture isolated Nginx error logs and Django/Gunicorn logs;
   inspect for the disposable token without printing it. Record which log paths
   contain it. The proposed logging correction needs separate review.

## Local validation results

Commands used `.venv/Scripts/python.exe`, explicit
`DJANGO_SETTINGS_MODULE=config.settings.test`, and `pytest -p no:cacheprovider`
with the configured fixed scratch base. An initial test attempt inherited the
shell's non-test settings and failed database setup; it was corrected by
explicitly selecting the required test settings. No database permissions or
roles were changed.

- Broad selection: `apps/accounts apps/audit apps/authorization apps/personnel
  apps/portal apps/file_library config/tests deploy/tests`: **705 passed,
  28 skipped, 10 setup errors**, 326.97 seconds. All ten errors come from
  `deploy/tests/test_credential_metadata_linux.py`, whose setUpClass explicitly
  requires Linux root. No test assertions failed. Linux-dependent skips and
  these native credential checks remain pending; no assertions were weakened.
- Focused selection after additional eligibility, reauthentication and address
  regressions: audit middleware, administrator lifecycle, personnel views,
  portal permissions/station boundaries and proxy deployment: **85 passed**,
  12.81 seconds. A final malformed-REMOTE_ADDR guard and four additional cases
  were verified separately: **25 audit middleware tests passed**, 6.49 seconds.
  Final authorization lifecycle/portal permissions rerun after the provisioning
  station/department consistency guard: **39 passed**, 10.70 seconds.
- Verification-only probes: **3 passed**, 8.55 seconds, reproducing the
  findings described above. They are not remediation acceptance tests.
- `ruff check .`: passed. `ruff format --check .`: passed (450 Python files).
- `mypy --platform linux .`: passed (419 source files). Default Windows mypy
  reports **21 platform errors in five unchanged deployment files**:
  `deploy/lib/configuration.py`, `deploy/lib/tls.py`, `deploy/lib/tls_backup.py`,
  `deploy/tests/test_configuration.py`, and
  `deploy/tests/test_credential_metadata_linux.py`. Changed Python files also
  pass the native Windows targeted type check.
- Configured Bandit scan (`-r apps config manage.py --exclude
  "*/tests/*,*/migrations/*"`): passed with zero findings.
- Existing advisory command `python tools/dependency_audit.py`: both
  requirements/base.txt and requirements/dev.txt scans reached their configured
  240-second timeouts. Advisory totals are **unknown**, not clean. The sanitized
  summary is retained in the ignored local `dependency-audit/summary.md`;
  rerun the advisory job with working resolver/advisory connectivity.
- Django `check`: passed. `makemigrations --check --dry-run`: no changes.
- Read-only `git diff --check`: passed. Git inspection used command-scoped
  safe.directory because the sandbox account differs from the workspace owner;
  no Git configuration, index or refs were changed.

Linux-only shell/systemd/Nginx checks and CI's disposable production-role
deployment bootstrap remain native Linux acceptance requirements.

## Exact changed files

- `apps/audit/middleware.py`
- `apps/audit/tests/test_middleware.py`
- `apps/authorization/services.py`
- `apps/authorization/tests/test_administrator_lifecycle.py`
- `apps/personnel/views.py`
- `apps/personnel/tests/test_views.py`
- `apps/portal/views.py`
- `apps/portal/tests/test_station_admin_boundaries.py`
- `config/settings/base.py`
- `config/settings/production.py`
- `config/tests/test_proxy_deployment.py` (new)
- `tools/phase_b_verification.py` (new, verification only)
- `docs/security-phase-b-remediation.md` (new)
