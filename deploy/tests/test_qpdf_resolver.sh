#!/usr/bin/env bash
# Focused regression tests for the managed qpdf resolver. These use a synthetic
# relocatable bundle; no network access or host package mutation is required.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
export FIREDASH_QPDF_VENDOR_ROOT="$TMPROOT/vendor/qpdf"
export CURL_LOG="$TMPROOT/curl.log"
BIN="$TMPROOT/bin"
mkdir -p "$BIN"

cat > "$BIN/dpkg" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    --print-architecture) printf 'amd64\n' ;;
    --compare-versions)
        # This test only compares qpdf versions with the fixed 12.4 floor.
        [[ $3 == ge && $4 == 12.4 ]] || exit 1
        major=${2%%.*}; minor=${2#*.}; minor=${minor%%.*}
        [[ $major -gt 12 || ( $major -eq 12 && $minor -ge 4 ) ]]
        ;;
esac
EOF
cat > "$BIN/qpdf" <<'EOF'
#!/usr/bin/env bash
if [[ $1 == --version ]]; then printf 'qpdf version 12.2.0\n'; exit 0; fi
if [[ $1 == --json-help ]]; then printf '{"encrypt":{},"streammethod":{},"stringmethod":{},"filemethod":{}}\n'; fi
EOF
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_LOG"
[[ ${CURL_FAIL:-0} != 1 ]] || exit 1
while [[ $# -gt 0 ]]; do
    if [[ $1 == -o ]]; then cp "$FAKE_ARCHIVE" "$2"; exit 0; fi
    shift
done
exit 1
EOF
chmod +x "$BIN/dpkg" "$BIN/qpdf" "$BIN/curl"
export PATH="$BIN:$PATH"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/qpdf.sh
source "$LIB_DIR/qpdf.sh"

make_bundle() {
    local root="$TMPROOT/bundle/$QPDF_VENDOR_ARCHIVE_ROOT"
    mkdir -p "$root/bin" "$root/lib"
    cat > "$root/bin/qpdf" <<'EOF'
#!/usr/bin/env bash
if [[ $1 == --version ]]; then printf 'qpdf version 12.4.1\n'; exit 0; fi
if [[ $1 == --json-help ]]; then printf '{"encrypt":{},"streammethod":{},"stringmethod":{},"filemethod":{}}\n'; fi
EOF
    chmod +x "$root/bin/qpdf"
    : > "$root/lib/libqpdf.so"
    export FAKE_ARCHIVE="$TMPROOT/$QPDF_VENDOR_ARCHIVE"
    (cd "$TMPROOT/bundle" && python3 -m zipfile -c "$FAKE_ARCHIVE" "$QPDF_VENDOR_ARCHIVE_ROOT")
    QPDF_VENDOR_SHA256=$(sha256sum "$FAKE_ARCHIVE" | awk '{print $1}')
}

failures=0
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }
assert() { "$@" || fail "$*"; }

[[ $QPDF_VENDOR_URL == "https://github.com/qpdf/qpdf/releases/download/v12.4.1/qpdf-12.4.1-bin-linux-x86_64.zip" ]] \
    || fail "pinned upstream qpdf URL is unexpected"
[[ $QPDF_VENDOR_SHA256 == "db9122e88ec00c76ac6a14e09ffb92406db1773d47b968911ff6e69f28c09bf9" ]] \
    || fail "pinned upstream qpdf checksum is unexpected"

make_bundle

[[ $QPDF_VENDOR_SHA256 == "$(sha256sum "$FAKE_ARCHIVE" | awk '{print $1}')" ]] \
    || fail "synthetic test bundle checksum was not installed"

# Debian-13-like system qpdf 12.2 falls back, creates the missing parent before
# mktemp, writes the selected runtime path, and retains the native binary.
resolved=$(resolve_qpdf)
[[ $resolved == "$QPDF_VENDOR_BIN" ]] || fail "old system qpdf did not select managed bundle"
[[ -x $QPDF_VENDOR_BIN ]] || fail "managed qpdf was not installed"
[[ $(read_secret "$FIREDASH_ETC/qpdf-path") == "$QPDF_VENDOR_BIN" ]] || fail "resolved qpdf path was not persisted"
[[ -x $BIN/qpdf ]] || fail "system qpdf was modified"
[[ -s $CURL_LOG ]] || fail "fallback did not download its pinned artifact"

# A valid managed installation is reused and does not download again.
: > "$CURL_LOG"
resolved=$(resolve_qpdf)
[[ $resolved == "$QPDF_VENDOR_BIN" ]] || fail "valid managed qpdf was not reused"
[[ ! -s $CURL_LOG ]] || fail "valid managed qpdf was downloaded again"

# A staging-directory failure stops before curl is invoked.
: > "$CURL_LOG"
rm -rf "$FIREDASH_QPDF_VENDOR_ROOT"
if (
    mktemp() { [[ $1 == *qpdf-stage* ]] && return 1; command mktemp "$@"; }
    install_vendor_qpdf
); then
    fail "staging failure unexpectedly succeeded"
fi
[[ ! -s $CURL_LOG ]] || fail "staging failure attempted a download"

# A failed download removes only its staging area and leaves an existing target
# untouched until a fully validated replacement is ready.
mkdir -p "$FIREDASH_QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION"
printf 'partial\n' > "$FIREDASH_QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION/keep"
: > "$CURL_LOG"
if (CURL_FAIL=1 install_vendor_qpdf); then
    fail "download failure unexpectedly succeeded"
fi
[[ -f $FIREDASH_QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION/keep ]] || fail "download failure changed existing installation"
if compgen -G "$FIREDASH_QPDF_VENDOR_ROOT/.qpdf-stage.*" >/dev/null; then
    fail "download failure left a staging directory"
fi

# Checksum and archive-layout failures are equally fail-closed and leave no
# staging directory or promoted target behind.
rm -rf "$FIREDASH_QPDF_VENDOR_ROOT"
if (QPDF_VENDOR_SHA256=not-the-approved-checksum install_vendor_qpdf); then
    fail "checksum mismatch unexpectedly succeeded"
fi
[[ ! -e $FIREDASH_QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION ]] || fail "checksum failure promoted an installation"
if compgen -G "$FIREDASH_QPDF_VENDOR_ROOT/.qpdf-stage.*" >/dev/null; then
    fail "checksum failure left a staging directory"
fi

malformed="$TMPROOT/malformed.zip"
printf 'not a zip\n' > "$malformed"
if (FAKE_ARCHIVE="$malformed" QPDF_VENDOR_SHA256="$(sha256sum "$malformed" | awk '{print $1}')" install_vendor_qpdf); then
    fail "malformed archive unexpectedly succeeded"
fi
[[ ! -e $FIREDASH_QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION ]] || fail "malformed archive promoted an installation"
if compgen -G "$FIREDASH_QPDF_VENDOR_ROOT/.qpdf-stage.*" >/dev/null; then
    fail "malformed archive left a staging directory"
fi

if [[ $failures -eq 0 ]]; then
    printf 'qpdf resolver tests passed\n'
else
    exit 1
fi
