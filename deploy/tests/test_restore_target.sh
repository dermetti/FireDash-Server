#!/usr/bin/env bash
# Substitute only the fixed restore root in a fixture copy; never restore host data.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export RESTIC_REPOSITORY=fixture RESTIC_PASSWORD_FILE=fixture FIRE_RESTORE_SNAPSHOT=fixture
export TRACE="$TMP/trace"
mkdir -p "$TMP/restore-tests" "$TMP/outside" "$TMP/bin"
sed "s|/var/lib/fire-backend/restore-tests|$TMP/restore-tests|g" "$ROOT/deploy/scripts/fire-restore" > "$TMP/restore"
cat > "$TMP/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TRACE"
EOF
chmod +x "$TMP/bin/restic"
export PATH="$TMP/bin:$PATH"
expect_rejected() {
    export FIRE_RESTORE_TARGET=$1
    if sh "$TMP/restore" > "$TMP/output" 2>&1; then
        echo "FAIL: accepted restore escape: $1" >&2; exit 1
    fi
    grep -q "${2:-FIRE_RESTORE_TARGET must be below}" "$TMP/output"
    [[ ! -e $TRACE ]]
}
expect_rejected "$TMP/restore-tests/../outside/new"
expect_rejected "$TMP/restore-tests/child/../.."
expect_rejected "$TMP/restore-tests/."
if ln -s "$TMP/outside" "$TMP/restore-tests/link" && [[ -L $TMP/restore-tests/link ]]; then
    expect_rejected "$TMP/restore-tests/link/new"
else
    echo 'SKIP: restore symlink escape check requires native Unix symlinks'
fi
mkdir -p "$TMP/restore-tests/nonempty"
touch "$TMP/restore-tests/nonempty/existing"
expect_rejected "$TMP/restore-tests/nonempty" 'FIRE_RESTORE_TARGET must be empty'
echo 'ok: restore refuses resolved escapes, the root itself and nonempty targets'
case "$(uname -s)" in
    MINGW*|MSYS*) echo 'SKIP: positive restore fixture requires Unix install mode handling'; exit 0 ;;
esac
export FIRE_RESTORE_TARGET="$TMP/restore-tests/valid"
sh "$TMP/restore"
grep -q -- "--target $TMP/restore-tests/valid" "$TRACE"
echo 'ok: valid isolated restore target reaches restic'
