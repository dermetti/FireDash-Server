#!/usr/bin/env bash
# Execute the verifier's actual application-check block with isolated command fixtures.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export REJECT="" TRACE="$TMP/trace"
RELEASE="$TMP/release"
ENV_FILE="$TMP/environment"
mkdir -p "$RELEASE/venv/bin"
: > "$ENV_FILE"
cat > "$RELEASE/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
echo "$2" >> "$TRACE"
[[ $2 != "$REJECT" ]]
EOF
chmod +x "$RELEASE/venv/bin/python"
# Extract only the real check block; no host-level verifier code is executed.
awk '/^\($/ { copy=1 } copy && /^# -------- systemd --------/ { exit } copy { print }' \
    "$ROOT/deploy/verify-deployment.sh" > "$TMP/checks"
[[ -s "$TMP/checks" ]]
fail() { FAIL=$((FAIL + 1)); }
ok() { :; }
load_env_file() { [[ -f $1 ]]; }
for REJECT in check migrate ''; do
    export REJECT
    FAIL=0
    source "$TMP/checks"
    if [[ -n $REJECT ]]; then [[ $FAIL -gt 0 ]]; else [[ $FAIL -eq 0 ]]; fi
done
rm "$ENV_FILE"
FAIL=0
source "$TMP/checks"
[[ $FAIL -gt 0 ]]
echo 'ok: deployment and migration check failures propagate to the verifier summary'

# Exercise the actual negative deletion probe with input available to its caller.
# The fixture fails immediately if rm could prompt or consume that input.
awk '/^    if runuser -u fire_publication -- test -w / { copy=1 } copy { print } copy && /^    fi$/ { exit }' \
    "$ROOT/deploy/verify-deployment.sh" > "$TMP/deletion-check"
[[ -s "$TMP/deletion-check" ]]
accepted_probe="$TMP/accepted"
runuser() {
    [[ $1 == -u && $2 == fire_publication && $3 == -- ]] || exit 1
    shift 3
    if [[ $1 == test ]]; then
        [[ $# -eq 3 && $2 == -w && $3 == "$accepted_probe/new.pdf" ]] || exit 1
        return 1
    fi
    [[ $# -eq 4 && $1 == rm && $2 == -f && $3 == -- && $4 == "$accepted_probe/new.pdf" ]] || exit 1
    if IFS= read -r unexpected_input; then
        exit 1
    fi
    printf 'deletion attempted\n' >> "$TRACE"
    return "$DELETION_STATUS"
}
for DELETION_STATUS in 1 0; do
    FAIL=0
    : > "$TRACE"
    source "$TMP/deletion-check" <<< 'input must never reach the deletion probe'
    [[ $(cat "$TRACE") == 'deletion attempted' ]]
    if [[ $DELETION_STATUS -eq 0 ]]; then
        [[ $FAIL -eq 1 ]]
    else
        [[ $FAIL -eq 0 ]]
    fi
done
echo 'ok: accepted-source deletion probe is non-interactive and successful deletion fails verification'
