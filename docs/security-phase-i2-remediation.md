# Phase I.2 CI regression correction

Baseline: `45edd0f9c668e3323ee637f7cc67a34468666bf4`.
Scope: the nested Nginx proxy handler regression and historical migration-test
compatibility. No application code, dependency, migration body or historical
reference artifact was changed.

## Changed files

| File | Correction |
| --- | --- |
| `deploy/nginx/fire-backend.conf` | Six explicit child-location `proxy_pass` directives. |
| `deploy/tests/test_tls.py` | Disposable synthetic HTTP upstream and native GET/POST delivery assertions. |
| `config/tests/test_migration_baseline.py` | Historical targets, approved forward set, exact index/catalog/record/recorder preservation and fresh/upgraded comparisons. |
| `docs/migrations/beta-baseline-phase2.md` | Clarifies that the immutable baseline can have approved forward extensions. |
| `docs/security-phase-i2-remediation.md` | This report. |

## Nginx correction

The Phase I nested locations inherited proxy settings but did not establish a
proxy content handler. Nginx therefore attempted local handling. Each tablet API,
exact report-submission, department import, dangerous-goods import, legacy/explorer
File Library and direct Fire Plan/KLGV child location now explicitly uses
`proxy_pass http://fire_backend` without a URI suffix.

The production upstream remains the protected Gunicorn Unix socket. Body limits,
ingestion override rendering, entry-rate policy, forwarded-header replacement,
HTTP version/timeouts, confidential logging, default listeners, ACME routing and
protected artifact ETags remain unchanged.

The existing native test still checks HTTP-01, redirects, exact body admission,
oversize rejection, shared entry-rate limiting and exemption of normal tablet
traffic. It now also substitutes a disposable loopback HTTP upstream for the test
only and verifies successful GET and POST delivery for every boundary route,
original path/query/body, HTTP/1.1 and replacement of spoofed forwarded headers.
Successful first login delivery must return 200 before rate rejection occurs.
No operational credentials, large payloads or host Nginx configuration are used.

## Historical migration compatibility

The tests previously treated current graph leaves as the historical schema and
assumed the production graph could never extend the original 16 nodes. They now
pin historical targets using the accepted mapping and allow only the explicitly
approved `tablets.0010_installation_credential_index` extension. All original source
hashes, acyclicity, absence of production replacements and exact file/graph
inventory checks remain enforced.

Historical counts remain 679 columns, 341 constraints, 296 indexes, 16 protected
functions, 20 triggers and 89 ownership entries. The historical catalog and
protected function hashes remain checked against the unchanged reference.

Actual PostgreSQL catalog assertions verify exactly these additional definitions:

```sql
CREATE INDEX tablets_appinstallation_credential_hash_c0a9352e
ON public.tablets_appinstallation USING btree (credential_hash);
CREATE INDEX tablets_appinstallation_credential_hash_c0a9352e_like
ON public.tablets_appinstallation USING btree (credential_hash varchar_pattern_ops);
```

The second index is Django's PostgreSQL CharField pattern-operator index. Current
schemas consequently have 298 indexes. The tests establish a historical database
first, require exactly the approved forward plan, apply it, and compare all other
catalog entries unchanged. Existing index ownership/ACLs and historical recorder
rows, including applied timestamps, must remain unchanged. Complete synthetic
record fingerprints include tablet identity, installation UUID and stored
credential hash alongside the existing publication, quota, audit and operator
fixtures. Fresh-current and upgraded-current catalogs must match. Subsequent
migrations must perform no writes and preserve the catalog, records and recorder.
The normalized schema-dump test now compares a real historical-to-current upgrade
against a fresh-current database.

The approved index migration, all 16 consolidated migration bodies and the three
historical JSON reference artifacts remain untouched. Existing seed, integrity
and tenant-authorization regressions remain intact.

## Verification

Validation used Windows, test settings and disposable local PostgreSQL fixtures.
No complete application suite or dependency advisory scans were run.

- Initial migration-baseline selection: **5 passed, 1 skipped** (54.34 seconds).
- Final combined baseline/tablet selection: **105 passed, 1 skipped** (73.65 seconds).
- Directly affected TLS tests: **138 passed, 37 skipped** (36.98 seconds).
- Full Linux-targeted `mypy --platform linux .`: **passed, 434 source files**.
- Changed-file Ruff and formatting: **passed**.
- `makemigrations --check --dry-run`: **passed, no changes detected**.
- Bash syntax and existing Nginx body-size/ETag shell regressions: **passed**.
- Whitespace validation: **passed**.

The migration selection uses `config/tests/test_migration_baseline.py`; the final
combined selection also includes tablet API, storage migration, lifecycle and
replacement tests. All pytest execution uses `config.settings.test` and
`-p no:cacheprovider`.

GitHub run [45641659114](https://github.com/dermetti/FireDash-Server/actions/runs/45641659114)
was inspected read-only: static checks, dependency audit and ACME environment
succeeded; server tests and deployment security failed, consistent with the
reported two migration assertions and native Nginx regression. No unrelated
correction was introduced or CI run triggered.

## Pending native verification

Native Nginx is unavailable locally; Linux-gated tests remain enabled for CI.
The skipped schema-dump comparison requires `pg_dump` (or the configured CI
PostgreSQL container client). Windows results do not establish native Nginx or
Debian deployment acceptance. Linux CI must execute the actual upstream-delivery,
body-admission and rate-limit test, and the PostgreSQL 17 normalized-dump
comparison. Existing native deployment acceptance requirements remain unchanged.
No deployment, existing-server access or Git mutations were performed.
