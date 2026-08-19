#!/usr/bin/env bash
# Regression test: Nginx's outer request-size boundary must sit modestly above
# the application's aggregate upload ceiling (MAX_INGEST_UPLOAD_BYTES = 256 MiB)
# so multipart framing overhead cannot clip a near-limit upload, and must never
# be tied to the individual PDF limit (MAX_PDF_INPUT_BYTES = 100 MiB).
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd)
TEMPLATE="$REPO_ROOT/deploy/nginx/fire-backend.conf"
VERIFY="$REPO_ROOT/deploy/verify-deployment.sh"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

grep -Eq '^[[:space:]]*client_max_body_size[[:space:]]+300m;[[:space:]]*$' "$TEMPLATE" \
    || fail "template client_max_body_size is not 300m"
if grep -Eq '^[[:space:]]*client_max_body_size[[:space:]]+256m;' "$TEMPLATE"; then
    fail "template ties client_max_body_size exactly to the upload ceiling (256m)"
fi
if grep -Eq '^[[:space:]]*client_max_body_size[[:space:]]+100m;' "$TEMPLATE"; then
    fail "template still ties client_max_body_size to the individual PDF limit (100m)"
fi
if grep -q 'match MAX_PDF_INPUT_BYTES' "$TEMPLATE"; then
    fail "template comment still ties client_max_body_size to MAX_PDF_INPUT_BYTES"
fi

grep -q 'Nginx client_max_body_size is 300m' "$VERIFY" \
    || fail "verifier does not check the 300m outer boundary"

if [[ $failures -eq 0 ]]; then
    echo "ok: Nginx outer boundary (300m) sits above the 256 MiB upload ceiling"
else
    exit 1
fi
