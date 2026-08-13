#!/usr/bin/env bash
# Lightweight test for classify_state under "pristine host" conditions:
# psql/postgres absent, no /etc/fire-backend, no /srv/firedash, no roles/database.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

# Redirect all on-host paths to a throwaway tree before sourcing the helpers.
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export FIREDASH_RELEASES_DIR="$TMPROOT/srv/firedash/releases"
export FIREDASH_CURRENT_LINK="$TMPROOT/srv/firedash/current"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/state.sh
source "$LIB_DIR/state.sh"

failures=0
assert_eq() {
    local expected=$1 actual=$2 label=$3
    if [[ $expected == "$actual" ]]; then
        echo "ok: $label"
    else
        echo "FAIL: $label (expected $expected, got $actual)" >&2
        failures=$((failures + 1))
    fi
}

# 1. Truly pristine: nothing exists anywhere (psql/postgres absent here).
assert_eq PRISTINE "$(classify_state)" "pristine host"

# 2. install.conf alone -> BOOTSTRAP_INCOMPLETE (must not be ESTABLISHED).
mkdir -p "$(dirname "$INSTALL_CONF")"
printf 'FIREDASH_INSTALL_SCHEMA_VERSION=1\n' > "$INSTALL_CONF"
assert_eq BOOTSTRAP_INCOMPLETE "$(classify_state)" "install.conf only"

# 3. secrets-initialized marker -> ESTABLISHED.
rm -f "$INSTALL_CONF"
mkdir -p "$(dirname "$SECRETS_MARKER")"
: > "$SECRETS_MARKER"
assert_eq ESTABLISHED "$(classify_state)" "secrets marker present"

# 4. current symlink -> ESTABLISHED (skipped where symlinks are unsupported).
rm -f "$SECRETS_MARKER"
mkdir -p "$(dirname "$CURRENT_LINK")"
if ln -s "/srv/firedash/releases/deadbeef" "$CURRENT_LINK" 2>/dev/null; then
    assert_eq ESTABLISHED "$(classify_state)" "current symlink present"
    rm -f "$CURRENT_LINK"
else
    echo "skip: current symlink (symlinks unsupported on this platform)"
fi

# 5. install.conf with LAST_SUCCESSFUL_SHA -> ESTABLISHED.
rm -f "$CURRENT_LINK"
printf 'FIREDASH_LAST_SUCCESSFUL_SHA=deadbeef\n' > "$INSTALL_CONF"
assert_eq ESTABLISHED "$(classify_state)" "install.conf with last successful sha"

# 6. env file with runtime password + db unknown -> ESTABLISHED (fail closed).
rm -f "$INSTALL_CONF"
mkdir -p "$(dirname "$ENV_FILE")"
printf 'POSTGRES_PASSWORD=abcdef\n' > "$ENV_FILE"
assert_eq ESTABLISHED "$(classify_state)" "env file present with db unknown"

if [[ $failures -eq 0 ]]; then
    echo "state classification tests passed"
else
    echo "state classification tests FAILED ($failures)" >&2
    exit 1
fi
