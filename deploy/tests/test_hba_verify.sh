#!/usr/bin/env bash
# Regression test: HBA verification must introspect pg_hba_file_rules via the
# postgres OS identity (not database_owner), keep database_owner non-superuser,
# and report an introspection failure distinctly from a bad-auth failure.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd)
VERIFY="$REPO_ROOT/deploy/verify-deployment.sh"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

# 1. A privileged postgres-identity helper exists.
grep -q 'pg_as_postgres' "$VERIFY" || fail "pg_as_postgres helper missing"

# 2. pg_hba_file_rules is queried via pg_as_postgres, not pg_as database_owner.
if grep -qE 'pg_as database_owner .*pg_hba_file_rules' "$VERIFY"; then
    fail "pg_hba_file_rules still queried as database_owner"
fi

# 3. database_owner is never granted superuser/extra catalog privileges.
if grep -qiE 'ALTER ROLE database_owner|database_owner.*(SUPERUSER|pg_read_all_settings)' "$VERIFY"; then
    fail "database_owner granted superuser/extra privileges in verifier"
fi

# 4. Introspection failure reports 'unable to inspect' (not 'not scram').
grep -q 'unable to inspect pg_hba_file_rules' "$VERIFY" || fail "missing 'unable to inspect' message"

# 5. Exact rule attributes are checked (db, address, /32 netmask, scram, error null).
grep -q 'fire_backend' "$VERIFY" || fail "missing database=fire_backend check"
grep -q '255.255.255.255' "$VERIFY" || fail "missing /32 netmask check"
grep -q 'scram-sha-256' "$VERIFY" || fail "missing scram-sha-256 check"
grep -qE '\-z \$err|\-z \$error' "$VERIFY" || fail "missing error IS NULL check"

# 6. Ordering check compares against a broad rule that could match (all/all + 127.0.0.1).
grep -q "ARRAY\['all'\]" "$VERIFY" || fail "missing broad-rule ordering comparison"

if [[ $failures -eq 0 ]]; then
    echo "ok: HBA verification uses postgres identity with distinct failure reporting"
else
    exit 1
fi
