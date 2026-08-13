#!/usr/bin/env bash
# Regression test: postgres_bootstrap must feed the SQL to psql on stdin (never -f),
# because psql runs as the postgres OS user and cannot traverse the root-only
# stage-0 checkout under /tmp. The SQL file itself sits beneath a root-only (0700)
# directory to mirror that condition.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export PSQL_CAPTURE="$TMPROOT/capture"

# Shim binaries that shadow the real runuser/psql on PATH.
BIN="$TMPROOT/bin"
mkdir -p "$BIN"

cat > "$BIN/runuser" <<'EOF'
#!/usr/bin/env bash
# Shim: strip "-u <user> --" and exec the remainder.
while (($#)); do
    case "$1" in
        -u) shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done
exec "$@"
EOF

cat > "$BIN/psql" <<'EOF'
#!/usr/bin/env bash
# Shim: record argv and stdin for assertions.
{
    printf 'ARGS:'
    printf ' <%s>' "$@"
    printf '\n'
    cat
} > "$PSQL_CAPTURE"
EOF
chmod +x "$BIN/runuser" "$BIN/psql"
export PATH="$BIN:$PATH"

# Secret/env files that postgres_bootstrap reads.
mkdir -p "$FIREDASH_ETC/credentials"
printf '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n' \
    > "$FIREDASH_ETC/credentials/database-owner-password"
cp "$FIREDASH_ETC/credentials/database-owner-password" \
    "$FIREDASH_ETC/credentials/backup-role-password"
printf 'POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n' \
    > "$FIREDASH_ETC/fire-backend.env"

# SQL under a root-only directory, mirroring the stage-0 mktemp checkout.
ROOTONLY="$TMPROOT/root-only"
mkdir -p "$ROOTONLY/deploy/postgresql"
printf -- '-- sentinel SQL content\n' > "$ROOTONLY/deploy/postgresql/bootstrap-production.sql"
chmod 700 "$ROOTONLY"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/postgresql.sh
source "$LIB_DIR/postgresql.sh"

postgres_bootstrap "$ROOTONLY/deploy/postgresql/bootstrap-production.sql"

if grep -q -- '<-f>' "$PSQL_CAPTURE"; then
    echo "FAIL: psql was invoked with -f (postgres must not open the SQL file directly)" >&2
    exit 1
fi
if ! grep -q -- '-- sentinel SQL content' "$PSQL_CAPTURE"; then
    echo "FAIL: psql did not receive the SQL on stdin" >&2
    exit 1
fi
echo "ok: postgres_bootstrap feeds SQL via stdin (no -f)"
