#!/usr/bin/env bash
# Trusted, fixed SQL probes only. Never pass user-controlled SQL to this helper.
expect_sqlstate() { # role password expected-sqlstate statement
    local role=$1 pw=$2 expected=$3 stmt=$4 out rc state
    # Even unexpected success is rolled back. ON_ERROR_STOP protects BEGIN and
    # timeout setup; disable it only for the probe so its SQLSTATE can be captured.
    # Disconnect also rolls back if psql/ROLLBACK fails. Do not emit server error
    # context, which can contain application rows; report only status and SQLSTATE.
    out=$(PGPASSWORD="$pw" psql -X -qAt -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 -U "$role" -d fire_backend 2>/dev/null <<SQL
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '3s';
\set ON_ERROR_STOP off
$stmt;
\echo FD_PROBE_SQLSTATE :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK;
SQL
    ) && rc=0 || rc=$?
    state=$(sed -n 's/^FD_PROBE_SQLSTATE \([0-9A-Z]\{5\}\)$/\1/p' <<<"$out")
    if [[ $rc -ne 0 || $state != "$expected" ]]; then
        fail "$role probe expected SQLSTATE $expected; got ${state:-unavailable} (psql status $rc)"
        return 1
    fi
    ok "$role probe denied (SQLSTATE $expected)"
}
