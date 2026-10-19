#!/usr/bin/env bash
# Isolated control-flow checks; no database, service, or host configuration touched.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export FIREDASH_ETC="$TMP/etc" FIREDASH_CURRENT_LINK="$TMP/current"
export FIREDASH_RELEASE="$TMP/release" FIREDASH_REPO_ROOT="$TMP/repo"
export TRACE="$TMP/trace" FAIL_STAGE=""
mkdir -p "$FIREDASH_ETC" "$FIREDASH_RELEASE/venv/bin" "$FIREDASH_REPO_ROOT/deploy/scripts" "$TMP/bin"
: > "$FIREDASH_ETC/fire-backend.env"
printf 'previous release\n' > "$FIREDASH_CURRENT_LINK"
cat > "$FIREDASH_REPO_ROOT/deploy/scripts/create-service-users.sh" <<'EOF'
#!/usr/bin/env bash
echo users >> "$TRACE"
EOF
cat > "$FIREDASH_RELEASE/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
stage=$2
[[ $stage != migrate || ${3:-} != --check ]] || stage=migration-check
[[ $stage != check ]] || stage=deployment-check
echo "$stage" >> "$TRACE"
[[ $stage != "$FAIL_STAGE" ]]
EOF
printf '#!%s/venv/bin/python\n' "$FIREDASH_RELEASE" > "$FIREDASH_RELEASE/venv/bin/gunicorn"
cat > "$TMP/bin/psql" <<'EOF'
#!/usr/bin/env bash
echo grants >> "$TRACE"
[[ $FAIL_STAGE != grants ]]
EOF
chmod +x "$FIREDASH_REPO_ROOT/deploy/scripts/create-service-users.sh" "$FIREDASH_RELEASE/venv/bin/"* "$TMP/bin/psql"
export PATH="$TMP/bin:$PATH"

attempt() {
    bash -Eeuo pipefail -c '
        source "$1/deploy/lib/release.sh"
        read_secret() { printf "fixture-password"; }
        activate_release
    ' bash "$ROOT" > "$TMP/output" 2>&1
}
expect_failure() {
    if attempt; then echo "FAIL: activation unexpectedly succeeded ($FAIL_STAGE)" >&2; exit 1; fi
    [[ $(cat "$FIREDASH_CURRENT_LINK") == 'previous release' ]]
    [[ ! -e "$FIREDASH_CURRENT_LINK.new" ]]
}

# Incomplete and conflicting markers must be rejected before mutable work.
expect_failure
[[ ! -e $TRACE ]]
touch "$FIREDASH_RELEASE/.firedash-release-complete" "$FIREDASH_RELEASE/.firedash-release-building"
expect_failure
[[ ! -e $TRACE ]]
rm "$FIREDASH_RELEASE/.firedash-release-building"
mv "$FIREDASH_RELEASE/venv/bin/gunicorn" "$TMP/gunicorn"
expect_failure
[[ ! -e $TRACE ]]
mv "$TMP/gunicorn" "$FIREDASH_RELEASE/venv/bin/gunicorn"

for FAIL_STAGE in migrate grants collectstatic deployment-check migration-check; do
    export FAIL_STAGE
    : > "$TRACE"
    expect_failure
    [[ $(tail -n1 "$TRACE") == "$FAIL_STAGE" ]]
done

# Exercise the real atomic symlink switch only when supported by this host.
FAIL_STAGE=""
if ln -s "$FIREDASH_RELEASE" "$TMP/probe" && [[ -L $TMP/probe ]]; then
    : > "$TRACE"
    attempt
    [[ $(readlink "$FIREDASH_CURRENT_LINK") == "$FIREDASH_RELEASE" ]]
    [[ $(cat "$TRACE") == $'users\nmigrate\ngrants\ncollectstatic\ndeployment-check\nmigration-check' ]]
    echo 'ok: successful prerequisites precede the atomic release switch'
else
    echo 'SKIP: real activation symlink check requires native Unix symlinks'
fi
echo 'ok: incomplete releases and failed activation prerequisites preserve current'
