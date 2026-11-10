#!/usr/bin/env bash
# Focused deployment regression tests for the outbound-mail HTTPS proxy value.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd)
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
export FIREDASH_ETC="$TMPROOT/etc/fire-backend"
mkdir -p "$FIREDASH_ETC" "$TMPROOT/release/venv/bin"
ln -s "$ROOT/apps" "$TMPROOT/release/apps"
export PYTHONPATH="$TMPROOT/release${PYTHONPATH:+:$PYTHONPATH}"
cat > "$TMPROOT/release/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
exec "${FIREDASH_TEST_PYTHON:-python3}" "$@"
EOF
chmod +x "$TMPROOT/release/venv/bin/python"

# shellcheck source=../lib/common.sh
source "$ROOT/deploy/lib/common.sh"
# shellcheck source=../lib/secrets.sh
source "$ROOT/deploy/lib/secrets.sh"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
# Literal rendering/preservation is covered by test_configuration.py.
# Deployment validation invokes the exact parser used by the HTTP transport.
validate_outbound_mail_https_proxy "$TMPROOT/release" 'http://proxy.example.test:3128' \
    || fail "valid proxy was rejected"
if validate_outbound_mail_https_proxy "$TMPROOT/release" 'socks5://proxy.example.test:1080'; then
    fail "unsupported proxy scheme was accepted"
fi
if validate_outbound_mail_https_proxy "$TMPROOT/release" 'http:///missing-host'; then
    fail "hostless proxy was accepted"
fi

if grep -Eri '100\.64\.0\.8|mjblab|squid|tailscale|headscale' \
    "$ROOT/deploy/lib" "$ROOT/deploy/install-local.sh" "$ROOT/docs/configuration.md" "$ROOT/docs/deployment.md" >/dev/null; then
    fail "deployment contains environment-specific proxy infrastructure"
else
    search_status=$?
    [[ $search_status -eq 1 ]] || fail "proxy infrastructure search failed (status $search_status)"
fi

if [[ $failures -eq 0 ]]; then
    echo "ok: outbound-mail proxy deployment configuration is preserved and validated"
else
    exit 1
fi
