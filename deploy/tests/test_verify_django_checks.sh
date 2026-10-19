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
