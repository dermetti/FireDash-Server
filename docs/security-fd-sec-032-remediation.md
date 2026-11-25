# FD-SEC-032: Debian PDF syntax-check compatibility

Baseline: `9998080d3d5bf2c45d8664cdb6f688ac9a13a4b5`.
Confirmed medium-severity native deployment blocker. The owner reports that all
five baseline CI jobs passed, but the first Debian 13 development VM upgrade
failed mandatory PDF smoke verification after application readiness. All other
deployment-verifier checks passed. No VM or existing server was accessed during
this correction.

## Native evidence and cause

Two independent verifier executions reproduced this strict-mode worker failure:

```text
File "/usr/local/lib/fire-backend/pdf/pdf_validation.py", line 95, in _inspect_pdf
    if strict and (page_count == 0 or document.check_pdf_syntax()):
AttributeError: 'pikepdf._core.Pdf' object has no attribute 'check_pdf_syntax'
```

Reported installed packages were `python3-pikepdf 9.5.2+dfsg-1+b2` (runtime
9.5.2), `python3-magic 2:0.4.27-3`, and `qpdf 12.2.0-1`. The application virtual
environment uses pikepdf 10.3.0. The isolated worker intentionally uses Debian
system packages through `/usr/bin/python3 -I`, not that virtual environment.
All six recovery-mode smoke cases passed; the first strict benign case failed.
The broker correctly returned a generic infrastructure failure.

[Pikepdf 9 release notes](https://pikepdf.readthedocs.io/en/latest/releasenotes/version09.html)
document replacement of `Pdf.check()` with `Pdf.check_pdf_syntax()` in 9.10;
[pikepdf 10 release notes](https://pikepdf.readthedocs.io/en/latest/releasenotes/version10.html)
document removal of `check()`. Testing only the application wheel missed this
distribution-package API difference.

## Correction and changed files

- `apps/reference_data/pdf_validation.py`: a small availability-based helper
  prefers `check_pdf_syntax()` and falls back to `check()` only when the modern
  attribute is absent. Nonempty structural diagnostics still reject the PDF.
  Missing/noncallable APIs, unsupported return values and checker exceptions
  fail closed as infrastructure failures; exceptions retain their cause and
  never trigger a legacy retry. Strict zero-page rejection and
  `attempt_recovery=False` remain unchanged. Recovery mode skips this checker
  exactly as before.
- `apps/reference_data/tests/test_pdf_validation.py`: modern preference, legacy
  fallback, both APIs' structural diagnostics, missing APIs, invalid results,
  operational failures (including `PdfError` escaping the content-error
  handler), no fallback after modern failure, and real zero-page rejection.
  Existing synthetic prohibited-action, encrypted-document and navigation
  coverage remains intact.
- `apps/reference_data/tests/test_pdf_worker.py`: strict structural diagnostics
  reject at both input and normalized-output boundaries. Existing strict versus
  recovery, malformed-input, output-content, process failure and broker tests
  are retained.
- `.github/workflows/ci.yml`: the mandatory existing Debian 13 amd64 production
  dependency container additionally installs `python3-pikepdf`, `python3-magic`
  and `qpdf` (and `util-linux` for `runuser`). It reports actual package versions
  with `dpkg-query`, installs the unchanged worker and shared validator as
  root-owned files, and invokes the harness with `/usr/bin/python3 -I` as a
  disposable non-root identity. No system-Python pip packages are installed.
- `deploy/tests/native_pdf_worker.py`: synthetic PDFs exercise the actual worker
  subprocess and qpdf, real MIME detection, strict/recovery benign processing,
  damaged-xref strict rejection and recovery acceptance, malformed/encrypted/
  prohibited-action rejection, and bounded result metadata. Successful output
  is checked against its size, page count and hash, reopened with shared strict
  validation, and checked with native qpdf. The worker algorithm is not copied.
- `deploy/tests/test_pdf_sanitizer.py`: checks that mandatory Debian CI uses the
  distribution packages, non-root isolated interpreter and root-owned helpers.
  Existing required host-verifier and installation assertions remain intact.
- `docs/security-fd-sec-032-remediation.md`: this evidence and acceptance report.

The remaining shared validator APIs were reviewed: `open(attempt_recovery=...)`,
`is_encrypted`, `pages`, `Root`, `PasswordError`, `PdfError`, typed `Name`,
`Dictionary`, `Stream` and `Array` inspection, `objgen`, dictionary items and
array indexing. No further compatibility change was identified. The reported
Debian recovery runs and local application tests exercise these operations; the
new Debian job extends this to strict parsing and normalization on the actual
installed distribution package versions, which it logs rather than assumes.

## Focused verification

- PDF validation, worker, broker, sandbox and deployment contract tests:
  **148 passed, 7 skipped**. One skip is missing native qpdf; six require Linux
  descriptor, ownership, no-follow and FIFO semantics unavailable on Windows.
- Changed Python files: Ruff and formatting passed; Linux-targeted mypy passed
  for six relevant files, including the unchanged worker import boundary.
- Bandit passed for the production validator and native test harness (the latter
  excludes B101 test assertions, as ordinary test assertions are not production
  security enforcement). An initial scan including test assertions reported only
  B101 findings; no production security check was relaxed.
- Workflow YAML parsing, container-command Bash syntax and whitespace checks
  passed. No full application suite or dependency advisory scan was run.

## Remaining native acceptance

Docker's client is installed locally, but its daemon is unavailable. The new
Debian container regression has **not** executed locally; its package-version
output and unmocked results remain pending GitHub CI. Native qpdf/systemd
containment acceptance cannot be inferred from mocked Windows tests.

After owner installation of the corrected reviewed release, the mandatory host
deployment verifier must pass all strict and recovery PDF smoke cases. Retain
real broker/service identity, root-owned helpers, private network and filesystem
namespaces, credential separation, integrity/finalization checks, and existing
CPU/memory/task/time limits. Container execution supplements, and does not
replace, that native host acceptance. No successful corrected host acceptance
is claimed. Production dependency locks, database schema, broker protocol,
storage lifecycle and sandbox configuration are unchanged.
