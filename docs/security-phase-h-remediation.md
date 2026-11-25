# Phase H targeted remediation

Baseline: `a2c0227dc1c8b6f96a6e86e02d7f328be869b128`. Local implementation and
focused verification only; no deployment, installed-server access or Git mutation.

## Changed files (19)

- `.github/workflows/ci.yml`
- `apps/publications/tests/test_deployment_lanes.py`
- `config/settings/base.py`
- `config/tests/test_api_documentation.py` (new)
- `deploy/lib/release.sh`
- `deploy/tests/test_release_venv.sh`
- `requirements/base.txt`
- `requirements/production.lock` (new)
- `requirements/production-constraints.txt` (new)
- `tools/production_lock.py` (new)
- `tools/test_production_lock.py` (new)
- `tools/dependency_audit.py`
- `tools/test_dependency_audit.py`
- `docs/security.md`
- `docs/deployment.md`
- `docs/production-dependencies.md` (new)
- `docs/frontend-dependencies.md` (new)
- `docs/frontend-inventory.json` (new)
- `docs/security-phase-h-remediation.md` (this report, new)

## FD-SEC-004: historical findings

The November 2 installed-environment observation and all advisory identifiers
remain in `security.md`. The downloaded November 25 CI artifact 15029872195
(run 45628537680) contains completed resolved requirements scans: production
34 packages, development 73, zero advisory entries and no skips. Reconciliation
records certifi 2026.11.20, filelock 4.3.1, msgpack 1.2.4, sqlparse 0.6.1 and
urllib3 2.8.0; filelock/msgpack are absent from production. These results clear
the old unresolved status for those resolved sets only. They do not establish
patched-range verification for every historical advisory or remediation of the
old local environment, existing VM, isolated/adopted Certbot environment or
future beta host.

## FD-SEC-028: production reproducibility

The real pip 26.2.1 resolver generated the 36-package lock for CPython 3.13,
Debian 13 amd64 using binary Linux/universal wheels. All 34 baseline versions
were retained. The only additions are sidecar 2026.10.1 and packaging 26.4,
which actual Gunicorn wheel metadata requires but the baseline audit artifact
did not enumerate. No blanket upgrades occurred.

Generation used the baseline artifact's versions as constraints, explicit
manylinux_2_17/2_28 x86_64 targets and cp313/compatible abi3 wheels, validated
the Linux dependency/extra closure, and checked each SHA-256 against HTTPS PyPI
metadata and downloaded wheel bytes. Host-only Windows dependencies are excluded.
A second real pip download with `--require-hashes --only-binary=:all: --no-deps`
and the same target flags successfully verified all 36 distributions. The
`--no-deps` download isolates wheel/hash verification from pip's host marker
evaluation; the generator validates dependency closure separately. This is
actual target-wheel availability evidence, **not native Linux installation**.

Release construction installs the complete lock with hash verification and
binary-only policy, then runs `pip check` before completing the immutable
release. Final-path environments, interrupted-build markers and completed-release
reuse remain intact. Previously completed environments are never rebuilt.
Mandatory CI application/production-schema jobs install that lock first, then
development tools constrained to identical production versions and `pip check`.
Deployment-security additionally installs/imports the lock in a fresh Debian 13
amd64 container. No existing native security regressions were weakened.

The [generation/update procedure](production-dependencies.md) documents tooling,
reviewed constraints, hash provenance, platform limits and installed-host checks.
The Linux hash lock is deliberately not the Windows development requirements.
Bootstrap pip and Debian native dependencies remain separately inventoried tools.

## FD-SEC-029: local Swagger assets

Supported drf-spectacular 0.29.0 sidecar integration now serves JS, CSS and favicon
through Django staticfiles/collectstatic. The added package is pinned and locked
at `drf-spectacular-sidecar==2026.10.1`, containing Swagger UI 5.33.1.
Its assets byte-match the versioned upstream npm distribution. Public schema
and documentation routes, authorization and existing static serving are retained.
Tests assert four discoverable local asset URLs, no external asset origins or
`@latest`, and a successful public OpenAPI 3.1 schema.

## FD-SEC-030: practical inventory and advisory reporting

Informational CI reporting now produces separate `production.json`, `dev.json`
and `acme.json` plus Markdown summary. Production and ACME enumerate their complete
locks with `--no-deps --disable-pip`; development retains resolution. Explicit
scan modes, bounded four-minute execution per set, sanitized diagnostics,
INCOMPLETE handling and independent mandatory quality checks are preserved.
Both locked inputs passed pip-audit parser/collection **dry runs** locally;
no new complete Python advisory scans were run here. The new complete three-set
advisory artifact remains pending CI. Existing ACME lock/environments, DNS plugin
approval, Certbot adoption and TLS lifecycle are unchanged.

The [frontend inventory](frontend-dependencies.md) records versions, upstream
sources, licenses and SHA-256 hashes for Bootstrap 5.3.3, Alpine 3.14.3, HTMX
2.0.6 and the four Swagger assets. All eight files match upstream distribution
bytes. Popper 2.11.8 and Vue reactivity/shared 3.1.5 are documented. The JSON
inventory enumerates 90 Swagger bundled package identities/versions/licenses
from source maps and the versioned upstream build lock.

Targeted OSV version review found no entries for those top-level frontend
versions or Popper/Vue. Bundle review identified **eight advisory entries**:
five low in DOMPurify 3.4.13 and three moderate in qs 6.15.2. IDs, severity,
reported fixed ranges, source links and applicability are retained. Swagger's
string/default sanitizer integration lacks the DOMPurify exploit preconditions.
The qs code runs in the documentation browser; adversarial Try-it-out inputs
and every client call path have not been exhaustively tested. These are reported
for release-owner review, not silently suppressed or claimed fixed/unreachable.
Fixes exist in upstream dependencies; a reviewed future sidecar/asset update can
incorporate them without editing compiled bundles or adding a JS build system.

## Baseline CI and minimal related correction

[Run 45628537680](https://github.com/dermetti/FireDash-Server/actions/runs/45628537680)
completed: static-checks, dependency-audit, acme-environment and deployment-security
passed; deployment-security logged **428 passed**. Server-tests logged
**1 failed, 2,196 passed, 1 skipped**. The sole failure was
`test_verifier_checks_lane_commands_socket_security_and_credential_separation`,
which expected a removed Phase G textual verifier message. The implementation
had moved credential enforcement into effective systemd verification.

That test now asserts the helper is wired and executes its real credential
checker against the maintenance unit, including a negative injected-KEK case.
Other assertions remain intact. The focused test passes; no production service,
PostgreSQL, Nginx or PDF checks were altered to address this failure.

## Focused verification

- **98 passed**, no skips/failures: production-lock consistency and negative
  marker/conflict/hash/drift cases; audit reporting/timeouts/incomplete artifacts;
  public Swagger/schema; publication deployment lanes; all effective systemd
  verifier regressions.
- Release venv shell regression: **passed**, covering final paths, hash/binary
  install arguments, `pip check`, failed install/check incomplete markers and
  completed-release reuse. Activation regression prerequisites **passed**;
  its native Unix symlink portion was **skipped** on Windows.
- 36 target wheels resolved and downloaded with verified hashes; offline lock
  consistency **passed**. Existing local Windows environment `pip check`
  **passed** after sidecar installation. This does not establish its advisory state.
- `manage.py spectacular --validate`: **passed**.
- Changed Python Ruff, formatting and mypy (`--platform linux`, seven files):
  **passed**. Bandit: no new findings; two existing low B404/B603 findings in
  the audit tool remain unchanged. New generator and changed settings are clear
  with narrowly documented fixed-command/HTTPS checks.
- Bash syntax for changed shell files and all CI run blocks: **passed**. Workflow
  YAML retains five jobs. ShellCheck 0.11.0: no added diagnostics; six existing
  release-helper and four existing shell-test diagnostics remain.
- Whitespace checks: **passed**.

No full application suite, repeated advisory scans, host changes or deployment
was performed. Docker daemon was unavailable and WSL access denied, so actual
Debian Python installation/imports and native service acceptance were not run.
Mocked service fixtures and Windows results are not native deployment acceptance.

## Remaining release acceptance

Require the new complete CI advisory artifacts, mandatory full regression jobs,
native Debian 13 amd64 hash install/imports/`pip check`, and review of the bundled
JavaScript findings. Record the actual installed beta environment inventory and
advisory review separately. Inventory Debian security/backports, system Python,
native PDF dependencies, OpenSSL, Nginx, PostgreSQL/PostGIS, geospatial libraries,
bundled wheel libraries and the selected installed Certbot/plugin environment.

Existing native acceptance remains required: exact reviewed-SHA fresh install,
repeated convergence/default-site reconciliation, controlled old-PDF transition,
real PDF processing/credential/filesystem containment, Nginx/Gunicorn client IP
attribution, setup/Magic-Link logging confidentiality, public HTTP-01 and managed
TLS staging/renewal, host firewall/SSH exposure and applicable backup/recovery
acceptance. Independently optional TLS backup acceptance remains optional.
Zero reported advisories is never a complete dependency-security guarantee.
