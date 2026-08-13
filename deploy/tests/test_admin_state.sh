#!/usr/bin/env bash
# Regression test: system_admin_state must run a quiet django.setup() snippet and
# capture exactly one token on stdout. It must not use `manage.py shell`, whose
# automatic-import banner ("N objects imported automatically") pollutes stdout.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export FIREDASH_RELEASE="$TMPROOT/release"
export PY_ARGS_LOG="$TMPROOT/py-args.log"

# Fake release venv python. It inspects the snippet and emits a banner on stderr
# (which must not contaminate the captured stdout token).
mkdir -p "$FIREDASH_RELEASE/venv/bin"
cat > "$FIREDASH_RELEASE/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$PY_ARGS_LOG"
snippet_file="${1:-}"
if [[ -z $snippet_file || ! -f $snippet_file ]]; then
    echo "FAIL: no snippet file passed" >&2
    exit 1
fi
if grep -q 'manage.py shell' "$snippet_file"; then
    echo "FAIL: snippet still uses manage.py shell" >&2
    exit 1
fi
if ! grep -q 'django.setup' "$snippet_file"; then
    echo "FAIL: snippet missing django.setup" >&2
    exit 1
fi
if ! grep -q 'classify_system_admin_state' "$snippet_file"; then
    echo "FAIL: snippet missing classify_system_admin_state" >&2
    exit 1
fi
# Django informational banner goes to stderr; must not contaminate stdout.
printf '38 objects imported automatically (use -v 2 for details).\n' >&2
printf 'none\n'
exit 0
EOF
chmod +x "$FIREDASH_RELEASE/venv/bin/python"

# Runtime env file loaded by system_admin_state.
mkdir -p "$FIREDASH_ETC"
printf 'POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n' \
    > "$FIREDASH_ETC/fire-backend.env"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/admin.sh
source "$LIB_DIR/admin.sh"

state=$(system_admin_state)

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

[[ $state == none ]] || fail "expected 'none', got '$state'"

args=$(cat "$PY_ARGS_LOG")
if [[ $args == *"manage.py"* || $args == *"shell"* ]]; then
    fail "system_admin_state still uses manage.py shell (args: $args)"
fi
[[ -n $args ]] || fail "system_admin_state passed no script path"

if [[ $failures -eq 0 ]]; then
    echo "ok: system_admin_state captures a single clean token (no shell)"
else
    exit 1
fi
