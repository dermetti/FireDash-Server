#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
SQL="$ROOT/deploy/postgresql/bootstrap-production.sql"
TEST_SQL="$ROOT/deploy/postgresql/bootstrap-test.sql"
VERIFY="$ROOT/deploy/verify-deployment.sh"
LIB="$ROOT/deploy/lib/postgresql.sh"

# shellcheck source=../lib/common.sh
source "$ROOT/deploy/lib/common.sh"
# shellcheck source=../lib/postgresql.sh
source "$ROOT/deploy/lib/postgresql.sh"

# Bootstrap is declarative SQL because the actual creation runs as the
# PostgreSQL cluster superuser. These assertions deliberately run under
# LANG=C: database creation must not inherit its encoding or locale from the
# installer process.
export LANG=C

grep -Fq "TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C.utf8'' LC_CTYPE ''C.utf8''" "$SQL"
grep -Fq "TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C.utf8'' LC_CTYPE ''C.utf8''" "$TEST_SQL"
grep -Fq "logical UTF-8 migration is required" "$SQL"
grep -Fq "recreate the disposable test template as UTF-8" "$TEST_SQL"
grep -Fq "pg_encoding_to_char(encoding)" "$VERIFY"
grep -Fq "UTF8|C.utf8|C.utf8" "$LIB"

firedash_database_locale_supported 'UTF8|C.utf8|C.utf8'
if firedash_database_locale_supported 'SQL_ASCII|C|C'; then
    echo "FAIL: SQL_ASCII database locale was accepted" >&2
    exit 1
fi

grep -Fq "WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'fire_backend')" "$SQL"
if grep -Fq 'DROP DATABASE' "$SQL" || grep -Fq 'ALTER DATABASE fire_backend' "$SQL"; then
    echo "FAIL: bootstrap must not mutate or replace an existing FireDash database" >&2
    exit 1
fi
if grep -Fq 'DROP DATABASE' "$TEST_SQL"; then
    echo "FAIL: test bootstrap must not replace an existing test template" >&2
    exit 1
fi

echo "ok: PostgreSQL bootstrap/verifier require UTF8 C.utf8 and fail closed"
