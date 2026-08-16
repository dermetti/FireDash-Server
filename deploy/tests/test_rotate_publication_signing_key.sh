#!/usr/bin/env bash
# Isolated regression coverage for the root-only signing-key rotation helper.
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HELPER="$ROOT/deploy/rotate-publication-signing-key"

(( EUID == 0 )) || {
    echo "SKIP: rotation helper integration test requires an isolated root-capable Linux environment" >&2
    exit 77
}

TMP=$(mktemp -d)
ORIGINAL_PATH=$PATH
PYTHON3=$(command -v python3)
trap 'rm -rf -- "$TMP"' EXIT

fail() { echo "rotation helper test failed: $*" >&2; exit 1; }

expect_failure() {
    local output
    if output=$("$@" 2>&1); then
        fail "expected failure: $*"
    fi
    printf '%s' "$output"
}

setup_case() {
    local name=$1 active_version=${2:-2}
    CASE="$TMP/$name"
    ETC="$CASE/etc/fire-backend"
    CREDS="$ETC/credentials"
    RELEASE="$CASE/release"
    SYSTEMCTL_LOG="$CASE/systemctl.log"
    SYSTEMCTL_FAIL_ONCE_FILE="$CASE/systemctl-failed-once"
    mkdir -p "$CREDS" "$RELEASE/venv/bin" "$CASE/bin"
    ln -s "$PYTHON3" "$RELEASE/venv/bin/python"
    : > "$SYSTEMCTL_LOG"
    cat > "$CASE/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:?}"
if [[ -n ${SYSTEMCTL_FAIL_ONCE_MATCH:-} && "$*" == *"$SYSTEMCTL_FAIL_ONCE_MATCH"* && ! -e ${SYSTEMCTL_FAIL_ONCE_FILE:?} ]]; then
    : > "$SYSTEMCTL_FAIL_ONCE_FILE"
    exit 1
fi
exit 0
EOF
    chmod 755 "$CASE/bin/systemctl"
    "$PYTHON3" - "$CREDS" "$active_version" <<'PY'
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

root, active = Path(sys.argv[1]), sys.argv[2]
keys = {}
for version, seed in (("1", b"1" * 32), ("2", b"2" * 32)):
    private = Ed25519PrivateKey.from_private_bytes(seed)
    keys[version] = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
active_seed = {"1": b"1" * 32, "2": b"2" * 32}[active]
active_private = Ed25519PrivateKey.from_private_bytes(active_seed)
(root / "publication-signing-key").write_bytes(active_seed)
(root / "publication-signing-public-key").write_bytes(active_private.public_key().public_bytes_raw())
(root / "publication-signing-public-key-ring.json").write_text(
    json.dumps({"keys": keys}, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii"
)
PY
    cat > "$ETC/fire-backend.env" <<EOF
POSTGRES_PASSWORD=not-used
DJANGO_SECRET_KEY=not-used
PUBLICATION_SIGNING_KEY_VERSION=$active_version
EOF
    chmod 700 "$CREDS"
    chmod 600 "$CREDS"/*
    chmod 640 "$ETC/fire-backend.env"
    export FIREDASH_ETC="$ETC" FIREDASH_RELEASE="$RELEASE" FIREDASH_CURRENT_LINK="$CASE/current"
    export SYSTEMCTL_LOG SYSTEMCTL_FAIL_ONCE_FILE
    unset SYSTEMCTL_FAIL_ONCE_MATCH
    export PATH="$CASE/bin:$ORIGINAL_PATH"
}

save_active_snapshot() {
    cp "$CREDS/publication-signing-public-key-ring.json" "$CASE/ring.before"
    cp "$CREDS/publication-signing-key" "$CASE/private.before"
    cp "$CREDS/publication-signing-public-key" "$CASE/public.before"
    cp "$ETC/fire-backend.env" "$CASE/env.before"
}

assert_active_unchanged() {
    cmp -s "$CASE/ring.before" "$CREDS/publication-signing-public-key-ring.json" || fail "ring changed"
    cmp -s "$CASE/private.before" "$CREDS/publication-signing-key" || fail "active private key changed"
    cmp -s "$CASE/public.before" "$CREDS/publication-signing-public-key" || fail "active public key changed"
    cmp -s "$CASE/env.before" "$ETC/fire-backend.env" || fail "environment changed"
}

assert_no_service_calls() {
    [[ ! -s $SYSTEMCTL_LOG ]] || fail "unexpected service operation: $(cat "$SYSTEMCTL_LOG")"
}

assert_no_success() {
    [[ $1 != *"prepared publication signing-key version"* ]] || fail "unexpected prepare success message"
}

# Exact live regression: valid active v2, retained v1/v2, and no staged v2.
setup_case duplicate 2
save_active_snapshot
ring_size_before=$(wc -c < "$CREDS/publication-signing-public-key-ring.json")
ring_sha_before=$(sha256sum "$CREDS/publication-signing-public-key-ring.json" | awk '{print $1}')
output=$(expect_failure bash "$HELPER" prepare --version 2)
[[ $output == *"version 2 is already retained"* ]] || fail "duplicate error is not precise: $output"
assert_no_success "$output"
assert_active_unchanged
[[ $(wc -c < "$CREDS/publication-signing-public-key-ring.json") == "$ring_size_before" ]] || fail "duplicate changed ring size"
[[ $(sha256sum "$CREDS/publication-signing-public-key-ring.json" | awk '{print $1}') == "$ring_sha_before" ]] || fail "duplicate changed ring digest"
[[ ! -e $CREDS/publication-signing-key-staging ]] || fail "duplicate created staging tree"
assert_no_service_calls

for invalid_case in empty malformed invalid-base64 invalid-length; do
    setup_case "$invalid_case" 2
    case "$invalid_case" in
        empty) : > "$CREDS/publication-signing-public-key-ring.json" ;;
        malformed) printf '{' > "$CREDS/publication-signing-public-key-ring.json" ;;
        invalid-base64) printf '%s\n' '{"keys":{"1":"not-base64"}}' > "$CREDS/publication-signing-public-key-ring.json" ;;
        invalid-length) printf '%s\n' '{"keys":{"1":"YQ=="}}' > "$CREDS/publication-signing-public-key-ring.json" ;;
    esac
    save_active_snapshot
    output=$(expect_failure bash "$HELPER" prepare --version 3)
    assert_no_success "$output"
    assert_active_unchanged
    [[ ! -e $CREDS/publication-signing-key-staging ]] || fail "$invalid_case created staging tree"
    assert_no_service_calls
done

setup_case invalid-version 2
save_active_snapshot
output=$(expect_failure bash "$HELPER" prepare --version 0)
[[ $output == *"positive integer"* ]] || fail "invalid-version error missing"
assert_no_success "$output"
assert_active_unchanged
assert_no_service_calls
output=$(expect_failure bash "$HELPER" prepare --version "$(printf '9%.0s' {1..65})")
[[ $output == *"positive integer"* ]] || fail "unsupported-version error missing"
assert_no_success "$output"
assert_active_unchanged
assert_no_service_calls

setup_case stage-conflict 2
mkdir -p "$CREDS/publication-signing-key-staging/3"
save_active_snapshot
output=$(expect_failure bash "$HELPER" prepare --version 3)
[[ $output == *"already has staged material"* ]] || fail "stage conflict error missing"
assert_no_success "$output"
assert_active_unchanged
assert_no_service_calls

# Simulate candidate generation failing after input validation but before any
# live credential replacement. No final staged version may appear.
setup_case generation-failure 2
REAL_PYTHON="$PYTHON3"
rm -f "$RELEASE/venv/bin/python"
cat > "$RELEASE/venv/bin/python" <<EOF
#!/usr/bin/env bash
for arg in "\$@"; do
    [[ \$arg == *"publication-signing-key-staging/.prepare."* ]] && exit 1
done
exec "$REAL_PYTHON" "\$@"
EOF
chmod 755 "$RELEASE/venv/bin/python"
save_active_snapshot
output=$(expect_failure bash "$HELPER" prepare --version 3)
assert_no_success "$output"
assert_active_unchanged
[[ ! -e $CREDS/publication-signing-key-staging/3 ]] || fail "generation failure committed staged version"
assert_no_service_calls

# Simulate the final ring rename failing. The valid ring and active credentials
# must remain unchanged; the uncommitted staged private pair is removed.
setup_case atomic-replace-failure 2
cat > "$CASE/bin/mv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${!#} == *"publication-signing-public-key-ring.json" ]]; then
    exit 1
fi
exec /bin/mv "$@"
EOF
chmod 755 "$CASE/bin/mv"
save_active_snapshot
output=$(expect_failure bash "$HELPER" prepare --version 3)
assert_no_success "$output"
assert_active_unchanged
[[ ! -e $CREDS/publication-signing-key-staging/3 ]] || fail "rename failure left staged version"
assert_no_service_calls

setup_case success-and-activate 2
prepared=$(bash "$HELPER" prepare --version 3)
[[ $prepared == *"prepared publication signing-key version 3"* ]] || fail "successful prepare did not report success"
grep -q '^try-restart fire-backend.service$' "$SYSTEMCTL_LOG" || fail "successful prepare did not refresh web"
"$PYTHON3" - "$CREDS/publication-signing-public-key-ring.json" <<'PY'
import json
import sys
assert set(json.load(open(sys.argv[1], encoding="ascii"))["keys"]) == {"1", "2", "3"}
PY
[[ -f $CREDS/publication-signing-key-staging/3/private ]] || fail "successful prepare did not stage private key"
[[ -f $CREDS/publication-signing-key-staging/3/public ]] || fail "successful prepare did not stage public key"
status_one=$(bash "$HELPER" status)
status_two=$(bash "$HELPER" status)
[[ $status_one == "$status_two" ]] || fail "repeated status is not harmless/stable"
private_b64=$(base64 -w0 "$CREDS/publication-signing-key")
[[ $status_one != *"$private_b64"* ]] || fail "status exposed private signing material"

# Mismatched staged public data must fail before worker quiescing or active
# credential mutation.
save_active_snapshot
: > "$SYSTEMCTL_LOG"
printf x > "$CREDS/publication-signing-key-staging/3/public"
output=$(expect_failure bash "$HELPER" activate --version 3)
[[ $output == *"invalid staged publication signing-key version 3"* ]] || fail "staged mismatch error missing"
assert_active_unchanged
assert_no_service_calls
"$PYTHON3" - "$CREDS/publication-signing-key-staging/3/private" "$CREDS/publication-signing-key-staging/3/public" <<'PY'
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
private, public = sys.argv[1:3]
seed = open(private, "rb").read()
open(public, "wb").write(Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw())
PY

# A one-time restart failure after replacement must restore the previous active
# v2 private/public/environment set and the expected worker state.
save_active_snapshot
: > "$SYSTEMCTL_LOG"
export SYSTEMCTL_FAIL_ONCE_MATCH="enable --now fire-publication-delivery.service"
output=$(expect_failure bash "$HELPER" activate --version 3)
[[ $output == *"prior active credentials and worker state restored"* ]] || fail "activation restoration error missing"
assert_active_unchanged
[[ -s $SYSTEMCTL_LOG ]] || fail "activation failure did not exercise mocked services"
unset SYSTEMCTL_FAIL_ONCE_MATCH

activated=$(bash "$HELPER" activate --version 3)
[[ $activated == *"activated publication signing-key version 3"* ]] || fail "activation did not report success"
grep -qx 'PUBLICATION_SIGNING_KEY_VERSION=3' "$ETC/fire-backend.env" || fail "active version did not change"
"$PYTHON3" - "$CREDS/publication-signing-public-key-ring.json" <<'PY'
import json
import sys
assert set(json.load(open(sys.argv[1], encoding="ascii"))["keys"]) == {"1", "2", "3"}
PY
: > "$SYSTEMCTL_LOG"
already_active=$(bash "$HELPER" activate --version 3)
[[ $already_active == *"already active; no change"* ]] || fail "already-active behavior is unclear"
assert_no_service_calls

echo "publication signing-key rotation helper tests passed"
