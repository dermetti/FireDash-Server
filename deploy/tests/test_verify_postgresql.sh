#!/usr/bin/env bash
# Shell contract regression; actual privilege enforcement is tested separately.
set -Eeuo pipefail
ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/verify-postgresql.sh
source "$ROOT/deploy/lib/verify-postgresql.sh"
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
export CAPTURE="$TMPROOT/input" RESPONSE=42501 PSQL_STATUS=0
fail() { echo "$*" >&2; }
ok() { :; }
psql() {
    [[ " $* " == *' -X '* ]] || return 99
    cat > "$CAPTURE"
    printf 'FD_PROBE_SQLSTATE %s\n' "$RESPONSE"
    return "$PSQL_STATUS"
}
expect_sqlstate application_runtime disposable 42501 'DELETE FROM audit_event'
grep -Fxq BEGIN\; "$CAPTURE"
grep -Fxq ROLLBACK\; "$CAPTURE"
grep -Fq "SET LOCAL lock_timeout = '1s'" "$CAPTURE"
grep -Fq "SET LOCAL statement_timeout = '3s'" "$CAPTURE"
for RESPONSE in 00000 42P01 42601 08006 'error text containing 42501' ''; do
    export RESPONSE
    if expect_sqlstate application_runtime disposable 42501 'DELETE FROM audit_event'; then
        echo "Unexpected probe acceptance: $RESPONSE" >&2; exit 1
    fi
done
export RESPONSE=42501 PSQL_STATUS=2
if expect_sqlstate application_runtime disposable 42501 'DELETE FROM audit_event'; then
    echo 'Connection failure accepted' >&2; exit 1
fi
echo 'OK: rollback boundary and exact SQLSTATE shell contract'
