# Phase 3.6.4: dependency remediation

Only three runtime pins changed: Django REST Framework **3.18.3**, pypdf
**6.21.4**, Pillow **13.0.0**. PyPI release JSON confirmed Python 3.13 support
and no listed advisories for these releases on 2026-11-02. DRF remains on its
3.18 patch line; pypdf is the minimum requested patched release; Pillow 13 is
the minimum requested security baseline. Django 5.2.17, ReportLab 4.5.1,
pikepdf 10.3.0, cryptography and development/type-stub pins remain unchanged.
DRF requires Django >=5.2; ReportLab requires Pillow >=9.0.0. Installation on
local Python 3.13.5 and `pip check` succeeded. The sole application-source edit
updates a pypdf version in a docstring; the matching private-API contract test
version assertion was updated without changing its encryption checks.

Release metadata: [DRF](https://pypi.org/pypi/djangorestframework/3.18.3/json),
[pypdf](https://pypi.org/pypi/pypdf/6.21.4/json),
[Pillow](https://pypi.org/pypi/Pillow/13.0.0/json).

## Audit modes and remaining findings

- Standard resolved requirement scans ran through `python tools/dependency_audit.py`:
  both base and dev scans timed out at their 240-second bounds. The runner exited
  successfully and generated a Markdown report marking both scans **INCOMPLETE**,
  with unknown totals, and no misleading clean result or raw resolver logs.
  These scans did not complete and are not counted as clean audits.
- Direct pinned base scan: 14 packages, zero findings, no skipped packages.
- Direct pinned dev scan: 10 packages, zero findings, no skipped packages.
  These use `pip-audit -r requirements/{base,dev}.txt --no-deps --disable-pip
  --cache-dir .audit-cache --timeout 15 --format json`. Nested base requirements
  are not included by this direct-only dev scan; both scans are necessary.
- Actual installed environment: `pip-audit --local --cache-dir .audit-cache
  --timeout 15 --format json`, 105 packages, no skips, 22 advisory entries across
  five other packages. All three upgraded packages have zero findings. This is
  a complete audit of the local environment, not a fresh Linux resolution.

Remaining findings below are intentionally not suppressed or upgraded in this
focused task. Local environment transitives may differ from a fresh CI install.
Fixes are advisory-reported versions, not verified compatible recommendations.
Review severity and reachability before staging/production approval; unknown
severity is not classified as harmless. No release-security approval is implied.

| Package | Installed | Advisory | Reported fix | Review status |
|---|---|---|---|---|
| certifi | 2026.7.22 | PYSEC-2026-4320 | 2026.10.18 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-v4x8-3wr5-3wq8 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-9vhv-jxww-xfm5 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-567q-5qvw-pf92 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-m462-7rxw-wvpg | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-j8f7-rjxc-mr56 | 4.0.9 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-8mx5-7m5m-r77g | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-69hj-f68j-2879 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-9c4x-gm8v-wgc4 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-rfpc-qcw5-3pc4 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-cwqr-hh33-gh44 | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-fq56-3cx3-fjxp | 4.2.1 | Open: reachability/severity review required |
| filelock | 3.32.2 | GHSA-6627-m3xw-93mp | 4.2.1 | Open: reachability/severity review required |
| msgpack | 1.2.1 | GHSA-j586-36cw-2gc2 | 1.2.3 | Open: reachability/severity review required |
| sqlparse | 0.5.5 | PYSEC-2026-3698 | 0.6.0 | Open: reachability/severity review required |
| sqlparse | 0.5.5 | PYSEC-2026-3697 | 0.6.0 | Open: reachability/severity review required |
| sqlparse | 0.5.5 | PYSEC-2026-3699 | 0.6.0 | Open: reachability/severity review required |
| sqlparse | 0.5.5 | PYSEC-2026-3696 | 0.6.0 | Open: reachability/severity review required |
| sqlparse | 0.5.5 | PYSEC-2026-3923 | 0.6.0 | Open: reachability/severity review required |
| urllib3 | 2.7.0 | PYSEC-2026-4177 | 2.8.0 | Open: reachability/severity review required |
| urllib3 | 2.7.0 | PYSEC-2026-4176 | 2.8.0 | Open: reachability/severity review required |
| urllib3 | 2.7.0 | PYSEC-2026-4175 | 2.8.0 | Open: reachability/severity review required |

## Compatibility validation

Focused isolated PostgreSQL/disposable-storage selection:

```powershell
$env:DJANGO_SETTINGS_MODULE = "config.settings.test"
.venv/Scripts/python.exe -m pytest -p no:cacheprovider `
  tools/test_dependency_audit.py `
  apps/tablets/tests/test_api.py `
  apps/tablets/tests/test_api_validation_logging.py `
  apps/tablets/tests/test_adoption_api_crypto.py `
  apps/report_transfer/tests `
  apps/outbound_mail/tests/test_report_admission.py `
  apps/outbound_mail/tests/test_report_delivery.py `
  apps/outbound_mail/tests/test_report_delivery_requests.py `
  apps/reference_data/tests/test_pdf_validation.py `
  apps/reference_data/tests/test_pdf_sandbox.py `
  apps/reference_data/tests/test_pdf_sanitizer_broker.py `
  apps/reference_data/tests/test_document_lifecycle.py `
  apps/publications/tests/test_pdf_bundles.py `
  apps/ingestion/tests/test_pdf_packages.py -q --tb=short --durations=10
```

Initial result: 400 passed, six native Linux sanitizer-broker cases skipped,
one failure, 131.96 seconds. The failure was the old explicit pypdf version
assertion. After updating it, reran the entire admission module and audit helper
module: **14 passed in 12.85 seconds**. Across the selection, all 401 distinct
retained cases passed; six platform cases remain unverified. No actual
compatibility regression was found, so no application behavior or security
limits changed. Coverage includes all Report Transfer tests, tablet credentials,
API parsing/error contracts, JWE/JPEG validation and size limits, thumbnails,
PDF generation, encrypted PDF-Mail, malformed PDF rejection and document lifecycle.

Other successful checks:

- `mypy .`: 413 source files.
- `ruff check .`; `ruff format --check .`: 531 files formatted.
- `bandit -r apps config manage.py --exclude "*/tests/*,*/migrations/*"`: no findings.
- `manage.py check`; `manage.py makemigrations --check --dry-run`.
- `manage.py spectacular --validate --fail-on-warn` (schema output in local cache).
- `python -m pip check`: no broken requirements.
- Both Report Transfer deployment Bash tests under Git Bash; credential/parser,
  lifecycle and verifier models pass. Explicitly selected the virtualenv Python
  using `FIREDASH_TEST_PYTHON` after Windows' default python3 alias failed.
  Native Unix symlink and root/service-identity ownership/access checks are
  explicitly skipped; require isolated Debian/LXC acceptance.
- Workflow YAML parsed and checked for independent advisory job and unchanged
  mandatory quality job; `git diff --check` passed.

Audit-helper regression tests cover advisory counts, clean results, missing or
invalid reports, skipped dependencies, inconsistent exit codes and timeout of
both scans. Infrastructure failures are never reported as zero vulnerabilities;
raw stderr (potential index credentials) is not logged or published.

No full local Django suite or live server operations were run. CI must still
execute the unchanged full PostgreSQL/PostGIS and OpenAPI gates. The independent
advisory job is informational even on installation/network failure; its artifact
and summary must be reviewed separately from quality-job success. No claim of
release readiness or production vulnerability clearance is made here.
