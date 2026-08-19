#!/usr/bin/env bash
# Regression test: the repo-managed PUBLICATION_ARTIFACT_MAX_BYTES ceiling
# converges to 600 MiB on render, is idempotent on rerun, and preserves an
# unrelated operator-controlled environment value.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
mkdir -p "$FIREDASH_ETC"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/secrets.sh
source "$LIB_DIR/secrets.sh"

install_file_atomic() {
    local src=$1 dst=$2
    cat "$src" > "$dst"
}

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

# Existing env: old canonical artifact ceiling + an operator-customized value.
cat > "$ENV_FILE" <<'EOF'
POSTGRES_PASSWORD=whatever
DJANGO_SECRET_KEY=whatever
PUBLICATION_ARTIFACT_MAX_BYTES=104857600
MAX_INGEST_UPLOAD_BYTES=157286400
EOF

render_env "" "" "firedash.test" >/dev/null 2>&1 || fail "render failed"
grep -qx 'PUBLICATION_ARTIFACT_MAX_BYTES=629145600' "$ENV_FILE" \
    || fail "artifact ceiling did not converge to 629145600"
grep -qx 'MAX_INGEST_UPLOAD_BYTES=157286400' "$ENV_FILE" \
    || fail "operator replacement value was overwritten"

before=$(cat "$ENV_FILE")
render_env "" "" "firedash.test" >/dev/null 2>&1 || fail "rerun render failed"
after=$(cat "$ENV_FILE")
[[ "$before" == "$after" ]] || fail "rerun is not idempotent"

rm -f "$ENV_FILE"
render_env "pw" "sk" "firedash.test" >/dev/null 2>&1 || fail "clean render failed"
grep -qx 'PUBLICATION_ARTIFACT_MAX_BYTES=629145600' "$ENV_FILE" \
    || fail "clean install did not render 629145600"

if [[ $failures -eq 0 ]]; then
    echo "ok: publication artifact ceiling converges to 600 MiB idempotently"
else
    exit 1
fi
