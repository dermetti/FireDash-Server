# Phase F database security remediation

Baseline: `cf27d6eb6d56ccc3c1f1a80011869013cd9201b4`.

## Changes

- FD-SEC-022: operational UPDATE/DELETE/TRUNCATE and registry INSERT privilege
  probes are replaced with non-mutating effective privilege checks. Audit trigger
  attachment and enablement checks remain. A statement-level `DELETE ... WHERE
  false` probe exercises the audit trigger without scanning or changing rows,
  including on an empty audit table. Every execution probe runs inside BEGIN /
  ROLLBACK, with one-second lock and three-second statement timeouts. A lost
  connection or psql failure also ends the transaction without commit. Exact
  SQLSTATE and psql exit status are checked; unexpected success, missing objects,
  syntax errors and connection failures fail verification. Diagnostics contain
  role/status/SQLSTATE rather than server error context or application rows.
- FD-SEC-023: runtime retains SELECT on `django_migrations`, while INSERT,
  UPDATE, DELETE and TRUNCATE are revoked after generic grants are reapplied.
  Owner migrations and ordinary application DML retain their existing grants.
- FD-SEC-024: backup retains SELECT on FireDash-owned sequences; USAGE and UPDATE
  are revoked on existing sequences and corresponding schema default privileges.
  Runtime and owner sequence access are unchanged. PostGIS-owned objects remain
  outside the grant loop.
- Shared effective privilege checks reject unsafe role attributes, unexpected
  direct/transitive memberships or SET ROLE capability, schema/database CREATE,
  migration/registry mutation, audit mutation, backup table writes (including
  column grants), and backup sequence mutation. Required reads, audit append,
  runtime ordinary DML and runtime sequence access are also checked. No
  administrator-managed membership or conflicting privilege is silently removed.

Service roles are expected to have no additional memberships. An existing custom
membership is reported as a verification failure and requires administrator
review. No new roles, migrations or application authorization rules were added.

## Changed files

- `deploy/verify-deployment.sh`
- `deploy/lib/verify-postgresql.sh`
- `deploy/postgresql/roles.sql`
- `deploy/postgresql/verify-privileges.sql`
- `deploy/tests/test_verify_postgresql.sh`
- `deploy/tests/test_database_privileges.sh`
- `.github/workflows/ci.yml`
- `docs/security-phase-f-remediation.md`

## Regression coverage and local results

The disposable production-schema CI fixture runs the real SQL regressions after
migrations. Tests cover repeated grant application, stale existing and default
sequence grants, runtime DML, migration and registry write denial, owner audit
trigger denial, backup sequence state reads and denied nextval/setval, owner and
runtime nextval, exact SQLSTATE checks, and unchanged rows after unexpectedly
successful UPDATE/DELETE/TRUNCATE probes. Transactional negative fixtures inject
unsafe grants, role memberships, missing migration history and schema creation
authority. Missing/disabled audit trigger behavior is covered using rolled-back
audit DDL and a disposable clone of the real trigger. The shared helper must
reject unexpectedly successful mutations against the clone and preserve its rows.

CI additionally runs runtime `manage.py migrate --check`. Complete custom-format
`pg_dump` and `pg_restore --list` remain mandatory against the migrated disposable
PostgreSQL 17/PostGIS database; they are not replaced by a schema-only dump.

Locally on Windows:

- `deploy/tests/test_configuration.py`: **180 passed, 1 skipped**. The skip is the
  existing native Linux inherited-flock-descriptor test.
- `test_verify_postgresql.sh`, `test_postgres_bootstrap.sh`, `test_hba_verify.sh`
  and `test_postgres_utf8.sh`: **all passed** under Git Bash. Expected negative
  probe diagnostics in the first script are assertions of correct rejection.
- Bash syntax checks: **passed** for all four changed/new shell files.
- ShellCheck 0.11.0: **passed** for the three new shell files. The existing
  deployment verifier still has baseline diagnostics; comparison found **no new
  diagnostics** (104 baseline, 90 current under the same invocation).
- CI YAML parsing and `git diff --check`: **passed**. No Python files changed, so
  Python formatting/Ruff/type checks are inapplicable.

No full application suite or dependency advisory scans were run. The baseline CI
success and counts were supplied in the review brief; these local changes have
not been submitted to CI.

## Pending native acceptance

This environment has no PostgreSQL CLI/server fixture and no usable Docker
engine. Actual database regressions and complete extraction were **not executed
locally**. The deployment-security job must execute
`deploy/tests/test_database_privileges.sh` against its disposable migrated
production-schema fixture, followed by the existing full dump/list checks and
runtime migrate check. The database script requires explicit
`FIREDASH_DISPOSABLE_DATABASE_TEST=1` and fixture-only owner/runtime/backup/admin
password environment variables. Never run it against an installed operational
database.

Native Debian 13/PostgreSQL 17/PostGIS deployment verification remains separate
acceptance. No deployment, operational database access or Git mutation occurred.
