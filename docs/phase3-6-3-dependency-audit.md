# Phase 3.6.3 dependency preflight

Bandit passes after replacing three production assertions with explicit invariant
guards and documenting the PDF output-escaping import exemption. Focused
PostgreSQL/storage/PDF/portal/audit tests: 238 passed in 171.84 seconds; additional
cleanup dry-run/counter tests: 2 passed in 11.96 seconds. The corrected mail-provider
scenario is included. Mypy (411 files), repository Ruff lint/format (529 files),
Django checks and OpenAPI validation with `--fail-on-warn` pass.

The exact base and development pip-audit commands were attempted but stalled
locally. A diagnostic traceback located the full-resolution stall in pip-audit's
isolated-environment pip upgrade. Default-cache access also stalled; a disposable
workspace cache allowed the explicit pinned-package scan below to complete.
This is not a completed transitive dependency audit. The supplemental development
file scan reported no findings, but `--no-deps` did not cover its included base
file; the explicit base scan is authoritative for the findings below.

Reproduction (exit status 1, 60 advisory entries across three pinned packages):

```text
pip-audit -r requirements/base.txt --no-deps --disable-pip --cache-dir .audit-cache --format markdown
```

Reported remediation versions: djangorestframework 3.18.0 to 3.18.2;
pypdf 6.19.0 to at least 6.21.4; Pillow 12.3.0 to 13.0.0. These are the audit's
fix versions, not an assertion of application compatibility after upgrading.
Package pins and CI gates remain unchanged. Resolve and validate those upgrades
separately before declaring dependency preflight green.

No full local suite or native Debian/LXC acceptance was run. Native POSIX fsync
execution and the complete published CI release gate remain required.

## Pinned-package audit output

Name | Version | ID | Fix Versions
--- | --- | --- | ---
djangorestframework | 3.18.0 | GHSA-33wh-fxxf-88vv | 3.18.2
djangorestframework | 3.18.0 | GHSA-3547-9m27-7rxg | 3.18.2
djangorestframework | 3.18.0 | GHSA-xw6w-gcfp-cf8m | 3.18.2
pypdf | 6.19.0 | GHSA-qfv9-64jg-mh7p | 6.20.1
pypdf | 6.19.0 | GHSA-7mh2-4gg2-979f | 6.20.0
pypdf | 6.19.0 | GHSA-9mx8-4j7g-633p | 6.20.1
pypdf | 6.19.0 | GHSA-39mj-jf6j-2qj3 | 6.20.0
pypdf | 6.19.0 | GHSA-5hx5-vpf9-5wcv | 6.21.1
pypdf | 6.19.0 | GHSA-cgf9-327m-v77c | 6.21.0
pypdf | 6.19.0 | GHSA-f54x-cmq8-qg57 | 6.21.0
pypdf | 6.19.0 | GHSA-6vvc-x485-93v7 | 6.21.0
pypdf | 6.19.0 | GHSA-6c7f-9265-g8fw | 6.21.2
pypdf | 6.19.0 | GHSA-jr96-gvgh-j3r4 | 6.21.2
pypdf | 6.19.0 | GHSA-gmh7-3g9r-mjh5 | 6.21.2
pypdf | 6.19.0 | GHSA-62m5-6wh8-28x7 | 6.21.3
pypdf | 6.19.0 | GHSA-42gh-23hr-vphw | 6.21.3
pypdf | 6.19.0 | GHSA-x3rh-gjgm-fmqc | 6.21.3
pypdf | 6.19.0 | GHSA-4vqv-7265-gm2w | 6.21.3
pypdf | 6.19.0 | GHSA-5849-834g-hvw5 | 6.21.4
pypdf | 6.19.0 | GHSA-mfr5-77xh-9j7h | 6.21.4
pypdf | 6.19.0 | GHSA-cggf-277g-3q54 | 6.21.4
pillow | 12.3.0 | PYSEC-2026-4327 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4328 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4326 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4339 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4340 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4343 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4334 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4337 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4341 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4330 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4338 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4335 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4336 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4331 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4332 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4329 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4333 | 13.0.0
pillow | 12.3.0 | PYSEC-2026-4342 | 13.0.0
pillow | 12.3.0 | GHSA-p4wg-p73j-c528 | 13.0.0
pillow | 12.3.0 | GHSA-65qm-jvmh-6pqg | 13.0.0
pillow | 12.3.0 | GHSA-q45j-58j2-45rg | 13.0.0
pillow | 12.3.0 | GHSA-462x-pgg6-55fp | 13.0.0
pillow | 12.3.0 | GHSA-c6hw-5grf-ff96 | 13.0.0
pillow | 12.3.0 | GHSA-vx6w-vv9f-6gvg | 13.0.0
pillow | 12.3.0 | GHSA-w889-685w-c8fh | 13.0.0
pillow | 12.3.0 | GHSA-j4m9-6w36-pw47 | 13.0.0
pillow | 12.3.0 | GHSA-fc9g-5qg7-mf45 | 13.0.0
pillow | 12.3.0 | GHSA-22hg-r758-rrw9 | 13.0.0
pillow | 12.3.0 | GHSA-74r2-7ff9-mjj2 | 13.0.0
pillow | 12.3.0 | GHSA-254f-3936-288h | 13.0.0
pillow | 12.3.0 | GHSA-jg86-v3jv-hrpm | 13.0.0
pillow | 12.3.0 | GHSA-f2g6-542v-9j58 | 13.0.0
pillow | 12.3.0 | GHSA-5xm2-6676-qcp9 | 13.0.0
pillow | 12.3.0 | GHSA-gg2r-wvfm-q9rx | 13.0.0
pillow | 12.3.0 | GHSA-332p-7hrc-92wf | 13.0.0
pillow | 12.3.0 | GHSA-24rc-8hj2-v98c | 13.0.0
pillow | 12.3.0 | GHSA-mrfp-p49p-3hf2 | 13.0.0
pillow | 12.3.0 | GHSA-g7cr-3p24-7wf7 | 13.0.0
pillow | 12.3.0 | GHSA-78g4-xqwv-q3g8 | 13.0.0
