#!/usr/bin/env bash
# Regression test: deprecated environment variables must fail closed, and
# explicitly migrated replacement variables must be preserved across renders.
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

# The test sandbox has no fire_backend group; the rendered bytes are all we assert on.
install_file_atomic() {
    local src=$1 dst=$2
    cat "$src" > "$dst"
}

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

write_env() {
    printf 'POSTGRES_PASSWORD=whatever\nDJANGO_SECRET_KEY=whatever\n' > "$ENV_FILE"
    local line
    for line in "$@"; do
        printf '%s\n' "$line" >> "$ENV_FILE"
    done
}

render() { render_env "" "" "firedash.test"; }

# 1. no deprecated variables -> render succeeds
write_env
render >/dev/null 2>&1 && echo "ok: clean render succeeds" || fail "clean render failed"

# 2. MAX_PDF_PACKAGE_BYTES present -> fail closed, name value + replacement
write_env 'MAX_PDF_PACKAGE_BYTES=157286400'
out=$(render 2>&1) && fail "MAX_PDF_PACKAGE_BYTES did not fail closed"
grep -q 'MAX_PDF_PACKAGE_BYTES=157286400' <<<"$out" || fail "missing deprecated name/value"
grep -q 'MAX_INGEST_UPLOAD_BYTES' <<<"$out" || fail "missing replacement name"

# 3. MAX_PDF_PACKAGE_MEMBERS present -> fail closed, name value + replacement
write_env 'MAX_PDF_PACKAGE_MEMBERS=1000'
out=$(render 2>&1) && fail "MAX_PDF_PACKAGE_MEMBERS did not fail closed"
grep -q 'MAX_PDF_PACKAGE_MEMBERS=1000' <<<"$out" || fail "missing deprecated name/value"
grep -q 'MAX_PDF_PACKAGE_DOCUMENTS' <<<"$out" || fail "missing replacement name"

# 4. both present -> fail closed and report both
write_env 'MAX_PDF_PACKAGE_BYTES=157286400' 'MAX_PDF_PACKAGE_MEMBERS=1000'
out=$(render 2>&1) && fail "both deprecated variables did not fail closed"
grep -q 'MAX_PDF_PACKAGE_BYTES' <<<"$out" || fail "missing MAX_PDF_PACKAGE_BYTES"
grep -q 'MAX_PDF_PACKAGE_MEMBERS' <<<"$out" || fail "missing MAX_PDF_PACKAGE_MEMBERS"

# 5. explicit replacement variables -> values preserved
write_env 'MAX_INGEST_UPLOAD_BYTES=157286400' 'MAX_PDF_PACKAGE_DOCUMENTS=42'
render >/dev/null 2>&1 || fail "replacement render failed"
grep -qx 'MAX_INGEST_UPLOAD_BYTES=157286400' "$ENV_FILE" || fail "MAX_INGEST_UPLOAD_BYTES not preserved"
grep -qx 'MAX_PDF_PACKAGE_DOCUMENTS=42' "$ENV_FILE" || fail "MAX_PDF_PACKAGE_DOCUMENTS not preserved"

# 6. rerun with only replacement variables -> identical effective output
before=$(cat "$ENV_FILE")
render >/dev/null 2>&1 || fail "rerun render failed"
after=$(cat "$ENV_FILE")
[[ "$before" == "$after" ]] || fail "rerun changed effective output"

if [[ $failures -eq 0 ]]; then
    echo "ok: deprecated env variables fail closed and replacements are preserved"
else
    exit 1
fi
