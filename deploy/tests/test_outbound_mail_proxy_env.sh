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

# The test sandbox has no deployment group; retain the atomic byte semantics.
install_file_atomic() {
    local src=$1 dst=$2
    local tmp
    tmp=$(mktemp "${dst}.tmp.XXXXXX")
    cat "$src" > "$tmp"
    mv -f "$tmp" "$dst"
}

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
render() { render_env "password" "django-secret" "firedash.test"; }

# No configured proxy is explicit direct egress in the runtime environment.
unset FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY || true
render >/dev/null || fail "default render failed"
grep -qx 'OUTBOUND_MAIL_HTTPS_PROXY=' "$ENV_FILE" || fail "default proxy is not direct"

# An established runtime value is preserved while install.conf is introduced.
sed -i 's|^OUTBOUND_MAIL_HTTPS_PROXY=$|OUTBOUND_MAIL_HTTPS_PROXY=http://proxy.example.test:3128|' "$ENV_FILE"
unset FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY || true
render >/dev/null || fail "preserving established proxy failed"
grep -qx 'OUTBOUND_MAIL_HTTPS_PROXY=http://proxy.example.test:3128' "$ENV_FILE" \
    || fail "established proxy was not preserved"

# The canonical installer value is rendered for the systemd EnvironmentFile.
export FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY='https://proxy.example.test:8443'
render >/dev/null || fail "configured proxy render failed"
grep -qx 'OUTBOUND_MAIL_HTTPS_PROXY=https://proxy.example.test:8443' "$ENV_FILE" \
    || fail "configured proxy was not rendered"
grep -q '^EnvironmentFile=/etc/fire-backend/fire-backend.env$' "$ROOT/deploy/systemd/fire-backend.service" \
    || fail "backend service does not load the rendered environment"

# Deployment validation invokes the exact parser used by the HTTP transport.
validate_outbound_mail_https_proxy "$TMPROOT/release" 'http://proxy.example.test:3128' \
    || fail "valid proxy was rejected"
if validate_outbound_mail_https_proxy "$TMPROOT/release" 'socks5://proxy.example.test:1080'; then
    fail "unsupported proxy scheme was accepted"
fi
if validate_outbound_mail_https_proxy "$TMPROOT/release" 'http:///missing-host'; then
    fail "hostless proxy was accepted"
fi

if rg -qi '100\.64\.0\.8|mjblab|squid|tailscale|headscale' \
    "$ROOT/deploy/lib" "$ROOT/deploy/install-local.sh" "$ROOT/docs/configuration.md" "$ROOT/docs/deployment.md"; then
    fail "deployment contains environment-specific proxy infrastructure"
fi

if [[ $failures -eq 0 ]]; then
    echo "ok: outbound-mail proxy deployment configuration is preserved and validated"
else
    exit 1
fi
