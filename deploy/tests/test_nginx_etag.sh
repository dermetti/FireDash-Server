#!/usr/bin/env bash
# Regression test: the internal X-Accel dataset location must preserve Django's
# cryptographic artifact ETag and the deployment verifier must enforce it.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd)
TEMPLATE="$REPO_ROOT/deploy/nginx/fire-backend.conf"
VERIFY="$REPO_ROOT/deploy/verify-deployment.sh"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

protected_dataset_location=$(awk '
    $1 == "location" && $2 == "/internal-protected-datasets/" && $3 == "{" {
        in_location = 1
        next
    }
    in_location && $1 == "}" { exit }
    in_location { print }
' "$TEMPLATE")

[[ -n $protected_dataset_location ]] || fail "protected dataset location missing from template"
grep -Eq '^[[:space:]]*internal;[[:space:]]*$' <<<"$protected_dataset_location" \
    || fail "protected dataset location is not internal"
grep -Eq '^[[:space:]]*alias[[:space:]]+/var/lib/fire-backend/publications/;[[:space:]]*$' \
    <<<"$protected_dataset_location" \
    || fail "protected dataset alias is not canonical"
grep -Eq '^[[:space:]]*etag[[:space:]]+off;[[:space:]]*$' <<<"$protected_dataset_location" \
    || fail "protected dataset static ETag is not disabled"
grep -Eq '^[[:space:]]*add_header[[:space:]]+ETag[[:space:]]+\$upstream_http_etag[[:space:]]+always;[[:space:]]*$' \
    <<<"$protected_dataset_location" \
    || fail "protected dataset does not preserve the upstream ETag"
etag_off_count=$(grep -Ec '^[[:space:]]*etag[[:space:]]+off;[[:space:]]*$' "$TEMPLATE")
[[ $etag_off_count -eq 1 ]] || fail "static ETags are disabled outside the protected dataset location"

# Keep the installed-config verifier coupled to every required protection.
grep -q 'Nginx protected dataset location is internal' "$VERIFY" \
    || fail "verifier does not check internal protection"
grep -q 'Nginx protected dataset alias is canonical' "$VERIFY" \
    || fail "verifier does not check canonical alias"
grep -q 'Nginx protected dataset static ETag disabled' "$VERIFY" \
    || fail "verifier does not check static ETag suppression"
grep -q 'Nginx protected dataset preserves upstream ETag' "$VERIFY" \
    || fail "verifier does not check upstream ETag preservation"

if [[ $failures -eq 0 ]]; then
    echo "ok: protected dataset X-Accel ETag preservation is enforced"
else
    exit 1
fi
