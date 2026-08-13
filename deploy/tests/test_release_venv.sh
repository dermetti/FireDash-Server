#!/usr/bin/env bash
# Regression test: build_release must create the virtualenv at its FINAL path
# (never under the staging directory), so console-script shebangs (e.g.
# venv/bin/gunicorn -> venv/bin/python) resolve after construction.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export FIREDASH_RELEASES_DIR="$TMPROOT/releases"
export FIREDASH_CURRENT_LINK="$TMPROOT/current"
export FIREDASH_RESOLVED_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
export FIREDASH_REPO_ROOT="$TMPROOT/repo"
export VENV_CREATED_LOG="$TMPROOT/venv-created.log"

# Environment-agnostic shims (host may lack a "root" user, e.g. Git Bash).
install() {
    local target="" skip_next=0
    while [[ $# -gt 0 ]]; do
        if [[ $skip_next == 1 ]]; then skip_next=0; shift; continue; fi
        case "$1" in
            -m|-o|-g) skip_next=1 ;;
            -d|-*) : ;;
            *) target=$1 ;;
        esac
        shift
    done
    mkdir -p "$target"
    return 0
}
chown() { return 0; }

BIN="$TMPROOT/bin"
mkdir -p "$BIN"

# Shim git: emit a valid empty tar archive and the expected SHA.
cat > "$BIN/git" <<'EOF'
#!/usr/bin/env bash
while [[ $# -gt 0 && $1 == -C ]]; do shift 2; done
case "${1:-}" in
    archive) head -c 1024 /dev/zero ;;
    rev-parse) echo "$FIREDASH_RESOLVED_SHA" ;;
esac
exit 0
EOF

# Shim python3: for "-m venv <target>" create a fake venv at <target> whose
# gunicorn shebang embeds <target>/bin/python; other invocations are no-ops.
cat > "$BIN/python3" <<'EOF'
#!/usr/bin/env bash
if [[ $# -ge 3 && $1 == -m && $2 == venv ]]; then
    target="$3"
    mkdir -p "$target/bin"
    printf '#!/bin/sh\nexit 0\n' > "$target/bin/python"
    printf '#!/bin/sh\nexit 0\n' > "$target/bin/pip"
    printf '#!%s/bin/python\n' "$target" > "$target/bin/gunicorn"
    chmod +x "$target/bin/python" "$target/bin/pip" "$target/bin/gunicorn"
    printf '%s\n' "$target" > "$VENV_CREATED_LOG"
fi
exit 0
EOF
chmod +x "$BIN/git" "$BIN/python3"
export PATH="$BIN:$PATH"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/release.sh
source "$LIB_DIR/release.sh"

build_release

FINAL="$FIREDASH_RELEASES_DIR/$FIREDASH_RESOLVED_SHA"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

if [[ "$(cat "$VENV_CREATED_LOG")" != "$FINAL/venv" ]]; then
    fail "venv was not created at the final path (got: $(cat "$VENV_CREATED_LOG"))"
fi
[[ -e "$FINAL/.firedash-release-complete" ]] || fail "completion marker missing"
[[ ! -e "$FINAL/.firedash-release-building" ]] || fail "building marker not removed"
[[ -x "$FINAL/venv/bin/gunicorn" ]] || fail "gunicorn not executable"

shebang=$(head -n1 "$FINAL/venv/bin/gunicorn")
interp="${shebang#\#!}"
interp="${interp%% *}"
if [[ $interp != "$FINAL/venv/bin/"* ]]; then
    fail "gunicorn shebang interpreter outside final venv: $interp"
elif [[ ! -e "$interp" ]]; then
    fail "gunicorn shebang interpreter does not exist: $interp"
fi

if [[ $failures -eq 0 ]]; then
    echo "ok: release venv is created at the final path and gunicorn resolves"
else
    exit 1
fi
