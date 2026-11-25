# Phase E.2 — FD-SEC-021

Baseline: `cf7b9860a247f827af0b94725d813677f7467fe7`.

## Correction

`apps/reference_data/pdf_sandbox.py` now sets the non-secret processing policy
`request.json` to `0644` instead of `0640`. Its comment now explains that the
output job directory is not setgid and the file retains the backend primary
group. Access remains bounded by the existing protected parent (`2710`) and
job (`0730`) directories. No directory permissions, group memberships, broker
interface, worker validation, output permissions or sandbox limits change.

## Regression coverage and files

- `apps/reference_data/tests/test_pdf_sandbox.py` checks the requested `0644`
  mode alongside the existing UUID-only broker request regression.
- `apps/reference_data/tests/test_pdf_permissions.py` exercises actual Linux
  file creation, setgid inheritance, chmod and discretionary access checks.
  Three distinct disposable numeric UID/GID process identities represent the
  backend, sanitizer and unrelated user; no host service accounts are changed.
  The real storage and policy-writing functions run as the backend. Only broker
  transport is replaced. The sanitizer can read but cannot directly write or
  chmod the policy. The unrelated identity cannot traverse protected directories
  or read policy/output files. Assertions preserve parent/job, quarantine and
  output/result modes. Privileged cleanup removes all identity-owned fixtures.
  Linux execution uses root or non-interactive sudo and skips only when native
  Linux or the necessary privileges/tools are unavailable.
- This report is the fourth changed file.

## Focused verification

Using `DJANGO_SETTINGS_MODULE=config.settings.test` and pytest
`-p no:cacheprovider`, the following focused files produced **75 passed,
7 skipped, zero failed** on Windows:

```
apps/reference_data/tests/test_pdf_sandbox.py
apps/reference_data/tests/test_pdf_permissions.py
apps/reference_data/tests/test_storage.py
apps/reference_data/tests/test_pdf_sanitizer_broker.py
deploy/tests/test_pdf_sanitizer.py
```

One skip is the new native Linux identity/permission regression; six are
existing Linux descriptor/ownership/FIFO regressions. Changed Python files
pass Ruff lint, formatting and Linux-platform mypy checks. No full suite or
dependency advisory scan was run locally.

The [baseline CI run](https://github.com/dermetti/FireDash-Server/actions/runs/45602779174)
completed successfully for the exact baseline SHA: static-checks, server-tests,
deployment-security, dependency-audit and acme-environment all passed. No
additional Phase E regression was identified. Production configuration and
native Nginx tests remain unchanged.

## Pending native acceptance

Run the focused permission regression on Linux with root or non-interactive
sudo and the project's Python environment. Windows skips are not native
permission acceptance. The test covers filesystem permissions, not systemd
mount isolation or resource enforcement.

Disposable Debian 13/systemd acceptance remains required: run the deployed
`verify_library_pdf_sanitizer` smoke check as `fire_backend` through deployment
verification, using the real broker and isolated service identities, and confirm
the worker reads its policy under the existing restrictions. All other native
acceptance requirements in [Phase E](security-phase-e-remediation.md) remain.
No deployment, migrations, existing-server access or Git mutations were performed.
