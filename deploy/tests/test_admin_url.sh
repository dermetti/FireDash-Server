#!/usr/bin/env bash
# Regression test: the initial-admin setup URL must be displayed at the very end
# only when a NEW administrator was created this run, and must never leak the raw
# URL/token to normal stdout.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export ADMIN_SETUP_URL_FILE="$TMPROOT/root/firedash-initial-admin-setup-url"
export ADMIN_CREATED_MARKER="$TMPROOT/run/firedash-admin-created"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/admin.sh
source "$LIB_DIR/admin.sh"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

mkdir -p "$(dirname "$ADMIN_SETUP_URL_FILE")" "$(dirname "$ADMIN_CREATED_MARKER")"

# Case 1: no marker -> no output, no-op.
rm -f "$ADMIN_CREATED_MARKER"
out=$(display_admin_setup_url)
[[ -z $out ]] || fail "no-marker case emitted stdout: $out"

# Case 2: marker set + URL file present -> URL never on stdout; marker cleared.
printf 'created=1\n' > "$ADMIN_CREATED_MARKER"
printf 'https://example.org/accounts/setup/SECRETTOKEN123/\n' > "$ADMIN_SETUP_URL_FILE"
chmod 600 "$ADMIN_CREATED_MARKER" "$ADMIN_SETUP_URL_FILE" 2>/dev/null || true
out=$(display_admin_setup_url)
if [[ $out == *SECRETTOKEN123* || $out == *accounts/setup* ]]; then
    fail "setup URL leaked to stdout: $out"
fi
[[ ! -e $ADMIN_CREATED_MARKER ]] || fail "marker not cleared after display"

# Case 3: marker with wrong content -> no output; marker cleared.
printf 'created=0\n' > "$ADMIN_CREATED_MARKER"
out=$(display_admin_setup_url)
[[ -z $out ]] || fail "wrong-marker case emitted stdout: $out"
[[ ! -e $ADMIN_CREATED_MARKER ]] || fail "wrong-marker not cleared"

# Case 4: source inspection — admin phase must not log the raw URL.
ADMIN_SRC="$LIB_DIR/admin.sh"
if grep -qE 'log(_warn|_err)? .*\$url' "$ADMIN_SRC"; then
    fail "admin.sh logs the raw setup URL variable"
fi
if ! grep -q 'created=1' "$ADMIN_SRC"; then
    fail "admin.sh does not set a created-this-run marker"
fi

if [[ $failures -eq 0 ]]; then
    echo "ok: setup URL is gated on created-this-run and never leaks to stdout"
else
    exit 1
fi
