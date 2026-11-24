# Phase B.2 security remediation

Baseline reviewed revision: `19e8abf7e047affd820b985e8142b5f93a54c2cf`.
The local tree is authoritative. Scope is FD-SEC-002 and FD-SEC-011 only.
The accepted FD-SEC-008/009/010 implementation and regression assertions remain.
No migrations, deployment, production access, Git mutations, cryptographic changes,
TLS lifecycle changes, or historical-data cleanup were performed.

## FD-SEC-002: setup credential confidentiality

The existing Report Transfer `TokenLogFilter` now also redacts setup URLs,
including malformed suffixes, query strings and percent-encoded paths. It handles
message arguments, nested mappings/lists/tuples, request/exception arguments,
structured extra fields, cached exception text and formatted exception chains.
It distinguishes HttpRequest diagnostics from development-server socket records;
non-request log records are still redacted without attempting request parsing.
Nested URL decoding is bounded; excessively encoded diagnostic strings are
replaced entirely. Ordinary messages and unrelated exception information remain.

The first middleware establishes request-local credential context before security,
CSRF, session and view processing. It extracts credentials from path/full URI,
raw URI and Referer, allowing even bare credentials in exception messages and
locals to be removed. ContextVars isolate concurrent requests and are reset on
exit. This does not modify the live request, token or routing.

Django console, request, CSRF, development-server and administrator error-email
handlers use the filter. The explicit administrator handler retains Django's
production-only email behavior, with redaction before subject/body construction.
Diagnostic request copies have sanitized paths, metadata, query values and
resolver arguments; their POST bodies are omitted without attempting to parse
rejected bodies. The original request continues through normal CSRF and setup
validation. The configured exception reporter redacts text/HTML output and hides
frame locals for setup-related requests, including failures before the view.
Unexpected exceptions retain Django's ordinary 500 outcome and sanitized stack
information; they are not converted to success or silently swallowed.

Gunicorn installs the shared filter in the master startup and worker hooks.
Access logging remains disabled; its fallback format still excludes URIs,
queries, Referer and user-agent. Error logging remains enabled and filtered.
The Unix-only listener, socket permissions and forwarded-IP replacement remain.

The Nginx setup prefix now has both access-log suppression and error-log discard,
equivalent to the existing Magic-Link prefix, including malformed setup suffixes.
Both HTTP and HTTPS servers explicitly serve as defaults for their IPv4/IPv6
listeners. HTTPS server-level error logging goes to `/dev/null`, since parser
failures and Referer/query credentials can occur before location selection and
Nginx cannot redact its error-log request lines. Its ordinary access log uses only
request ID, numeric status, byte count and duration. Setup and Magic-Link access
logs remain disabled. Ordinary application error logging remains enabled.

Setup and ordinary Referer forwarding is retained for HTTPS CSRF fallback;
Django diagnostics redact it. The existing Report Transfer Referer stripping and
portal outcomes remain unchanged. Successful setup still consumes its token,
activates the account, requires password authentication and proceeds to mandatory
MFA enrollment. Existing audit and request-ID behavior is retained.

## FD-SEC-011: atomic verification

`verify_totp` is the shared verification boundary for enrollment, login and
reauthentication. Its durable PostgreSQL transaction locks the account row first,
then loads the relevant persisted TOTP device with `select_for_update`. It checks
active user/MFA state under the account lock and delegates verification unchanged
to django-otp. Successful replay state, drift, last-used timestamps and throttle
state are saved while locked. Failed verification's django-otp throttle changes
also commit normally. Enrollment confirmation and `user.mfa_enabled` commit in
the same transaction as successful verification.

Views establish login sessions or recent-reauthentication timestamps only after
the service transaction exits successfully. A failed save or commit therefore
cannot publish success. Success audit actions retain their existing meanings
and occur only after committed verification. An audit/session failure after
verification consumes the code and fails closed; it does not make a consumed
code reusable.

First-device creation locks the same account row before checking confirmed
state or creating the existing `default` device. It never replaces a confirmed
device. Enrollment verification selects that default device. No new device
schema or custom TOTP implementation was introduced.

Account/IP throttles, django-otp tolerance/drift/replay/throttle behavior, mandatory
password authentication, pending-MFA expiry, session rotation and recent-reauth
expiry remain. Independent pending sessions share the same device replay state.

## Normal-discovery regressions

The former explicit reproduction tool was removed. Its tests now assert secure
behavior in `apps/accounts/tests/test_security_regressions.py`; no assertions
expecting a vulnerability remain.

New tests cover disposable setup GET and CSRF-valid POST, consumption/activation
and subsequent enrollment, invalid/expired tokens, CSRF rejection, render/service
and pre-view middleware exceptions, malformed/encoded paths, query and Referer
values, structured logging, diagnostic request copies, text/HTML traceback
reports and administrator error-email subject/body. Ordinary exception logging
and shared Report Transfer redaction remain covered.

PostgreSQL concurrency tests use independent connections and browser sessions.
Rendezvous happens before acquiring verification locks, never inside the locked
verification section. They cover exactly one accepted concurrent login and one
success audit event; concurrent reauthentication; shared login/reauth replay;
concurrent enrollment/confirmation and first-device creation; sequential reuse;
later valid codes; expired pending sessions; invalid-code/device throttling;
verification-save and actual transaction-commit failures; and refusal to run
inside an enclosing transaction. Failed requests gain neither authentication
nor a fresh reauthentication timestamp (existing reauth sessions remain logged in).

## Local validation

Final requested selection: **656 passed, 28 skipped, zero failures** in
288.99 seconds. Command (with test settings explicitly selected):

```powershell
$env:DJANGO_SETTINGS_MODULE='config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/accounts apps/audit apps/authorization apps/personnel apps/portal apps/report_transfer/tests/test_portal.py config/tests deploy/tests/test_configuration.py deploy/tests/test_tls.py -q --tb=short -x
```

This includes all accepted Phase B regressions. Skips require Linux native
symlink/permission/flock behavior or unavailable Nginx, Restic or pg_dump tools.
Native root credential metadata and Linux shell/systemd checks were not executed
on Windows and remain pending, rather than passed. The final development-server
socket-record guard is additionally covered by the focused rerun recorded below.

- Focused accounts/audit/proxy-deployment verification after fixing diagnostic
  request construction: **98 passed**, 31.90 seconds. The final socket-record compatibility rerun also passed
  **98 tests**, 32.47 seconds.
- `ruff check .`: passed. `ruff format --check .`: passed, 453 Python files.
- `mypy --platform linux .`: passed, 422 source files. Native Windows mypy
  against all changed Python components passed (31 source files). Full native
  Windows mypy reports the same **21 platform errors in five unchanged Linux
  deployment files**: `deploy/lib/configuration.py`, `deploy/lib/tls.py`,
  `deploy/lib/tls_backup.py`, `deploy/tests/test_configuration.py` and
  `deploy/tests/test_credential_metadata_linux.py`. No type suppressions added.
- Configured Bandit scan: passed with **zero findings**. Existing nosec-comment
  warnings are unchanged.
- Django `check`: passed. `makemigrations --check --dry-run`: no changes.
- `git diff --check`: passed; line-ending notices do not indicate whitespace
  failures. Command-scoped `safe.directory` was needed for read-only inspection;
  no Git configuration/index/ref was changed.
- `python tools/dependency_audit.py`: **INCOMPLETE**. Both base/dev requirement
  scans reached their configured 240-second timeouts. Advisory totals are
  **unknown**, not clean. The ignored local `dependency-audit/summary.md` records
  the sanitized results; rerun CI's advisory job with functioning connectivity.

During development, an initial broad run had 653 passed, 28 skipped and one
failure in an email-handler test double. A subsequent run had 651 passed,
28 skipped and five failures caused by attempting to shallow-copy Django's
non-copyable `ResolverMatch` while constructing diagnostic requests. Both were
corrected. The focused 98-test rerun above passed, including a new resolved-request
regression; no existing security assertions were weakened.

All pytest runs explicitly use `DJANGO_SETTINGS_MODULE=config.settings.test`,
the configured disposable PostgreSQL test database, `-p no:cacheprovider` and
the repository's guarded `.test-tmp`. No database roles were weakened.

## Native Linux acceptance still required

This Windows environment has no native Nginx executable or supported Gunicorn
Unix worker execution. Static/mock hook tests are not native deployment evidence.
Use an isolated disposable Linux host/container with Nginx and Gunicorn, a throwaway
certificate, test settings/database and the production Unix-socket ownership/mode.
Do not use an existing server, production credentials or operational data.

1. Render the template for the disposable host and run `nginx -t` and `nginx -T`.
   Confirm FireDash is the sole default on every intended IPv4/IPv6 listener,
   HTTP/HTTPS server error logs discard request-bearing diagnostics, and no
   inherited/custom access format records request, URI, args or Referer. Inspect
   the complete effective configuration, not just the setup location. Test
   absent/unknown Host and SNI, HTTP/1.0 requests and each address family. A
   conflicting pre-existing default must fail configuration validation and be
   resolved by the owner before deployment.
2. Start Gunicorn using both the ordinary Unix bind and systemd socket activation
   in separate checks. Verify socket owner/group/mode and lack of a public TCP
   listener. Capture master/worker journals, Gunicorn access/error output, Django
   console and disposable administrator diagnostic email, plus all default,
   global and per-site Nginx logs, without echoing credential values.
3. Issue a disposable setup token and verify GET, valid-CSRF HTTPS POST (including
   Referer-only fallback without Origin), subsequent password/MFA enrollment,
   invalid/expired/reused tokens and CSRF failure. Repeat with a setup URL in
   Referer and in a query to an ordinary route. Inject render and service faults;
   confirm ordinary fixed statuses, sanitized diagnostic presence and request-ID
   behavior, with no credential substring in any captured diagnostic.
4. Send raw malformed setup request lines/paths, encoded slashes and nested
   encodings, malformed/oversized headers, invalid percent escapes and rejected
   methods. Include failures occurring before location selection or Django
   middleware. Exercise HTTP redirect and unknown/default virtual-host paths.
   Simulate upstream refusal, timeout and worker termination using disposable
   workers. Search every captured log/report for each disposable credential
   with a quiet exact match; print only pass/fail, never matching content.
5. Repeat Magic-Link logging/error cases and ordinary non-sensitive 404/500
   diagnostics. Confirm Report Transfer protection and application error
   visibility remain. Run CI's Linux deployment/security checks, including native
   root credential metadata tests and shell/Nginx tests skipped on Windows.

Remaining external boundary: this repository cannot sanitize logs of another
explicitly selected virtual host, a distribution/global logger that executes
before the configured server is selected, or an operator-added logging handler.
Native effective-configuration and early-parser checks must demonstrate those
boundaries are protected before public beta. Do not treat local tests as closing
that acceptance requirement. External proxy/observability systems are likewise
outside the supplied configuration.

## Compatibility constraints

- The Nginx site now requires ownership of the default HTTP/HTTPS listeners.
  Duplicate defaults fail `nginx -t`; shared-host configurations need explicit
  owner review of every applicable logging boundary. It is an HTTP-context
  include, as before; the safe `log_format` is defined there.
- Nginx detailed request-bearing error logs are unavailable for these servers.
  Sanitized application diagnostics and the safe ordinary HTTPS access log remain.
  This is necessary for early-error confidentiality without a new logging module.
- `verify_totp` deliberately rejects an enclosing production transaction. Current
  settings use autocommit and no `ATOMIC_REQUESTS`; future callers/configurations
  must retain a committed verification boundary before session success. Django's
  own TestCase transaction wrapper remains supported by durable transactions.
- Installed django-stubs omits ResolverMatch captured/extra keyword attributes;
  diagnostic reconstruction reads these optional attributes with `getattr` and
  copies namespace lists. No type-check suppression or Django behavior change
  is required. Django development-server socket records are handled separately
  from HttpRequest records.
- Existing inconsistent device/user states fail closed: confirmed devices are
  never overwritten, and enrollment does not automatically repair disabled-MFA
  accounts that already have a confirmed device or duplicate default devices.
  No historical reconciliation or cleanup was performed.

## Exact changed files

- `apps/accounts/security.py` (new)
- `apps/accounts/services.py`
- `apps/accounts/views.py`
- `apps/accounts/tests/test_security_regressions.py` (new, migrated secure probes)
- `apps/accounts/tests/test_setup_confidentiality.py` (new)
- `apps/accounts/tests/test_totp_atomic.py` (new)
- `apps/report_transfer/security.py` (shared redaction only)
- `config/settings/base.py`
- `config/tests/test_proxy_deployment.py`
- `deploy/gunicorn/gunicorn.conf.py`
- `deploy/nginx/fire-backend.conf`
- `tools/phase_b_verification.py` (removed; regressions moved into discovery)
- `docs/security-phase-b-remediation.md` (link to this follow-up)
- `docs/security-phase-b2-remediation.md` (new)
