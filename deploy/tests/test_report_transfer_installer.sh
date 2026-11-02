#!/usr/bin/env bash
# Execute only the installer's actual phase sequence with disposable stubs.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export TRACE="$TMP/trace" REJECT=""
mkdir -p "$TMP/repo/deploy"
for script in bootstrap-lxc deploy-release initialize-firedash; do
    cat > "$TMP/repo/deploy/$script.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s %s\n' "${0##*/}" "${1:-}" >> "$TRACE"
[[ -z $REJECT || ${1:-} != "$REJECT" ]]
EOF
    chmod +x "$TMP/repo/deploy/$script.sh"
done
awk '/^# ============================== phases/ { copying=1; next }
     copying && /^FIREDASH_PHASE=start-socket/ { exit }
     copying { print }' "$ROOT/deploy/install-local.sh" > "$TMP/phases"
quiesce() { echo quiesce >> "$TRACE"; [[ $REJECT != quiesce ]]; }
postgres_bootstrap() { echo postgres >> "$TRACE"; }
install_nginx() { echo nginx >> "$TRACE"; }
attempt() (
    set -Eeuo pipefail
    ROOT="$TMP/repo"
    source "$TMP/phases"
)
: > "$TRACE"
attempt
[[ $(cat "$TRACE") == $'bootstrap-lxc.sh \ndeploy-release.sh build\nquiesce\ninitialize-firedash.sh secrets\npostgres\nnginx\ndeploy-release.sh activate' ]]
for REJECT in quiesce secrets; do
    : > "$TRACE"
    # A new bash retains errexit even when observed by a parent failure check.
    export -f quiesce postgres_bootstrap install_nginx
    if ROOT="$TMP/repo" bash -Eeuo pipefail "$TMP/phases"; then
        echo "FAIL: installer ignored $REJECT" >&2; exit 1
    fi
    ! grep -q '^postgres\|^nginx\|activate$' "$TRACE"
done
echo 'ok: report writers quiesce before secrets; failed prerequisites prevent migrations and activation'

# The real secrets entrypoint must include RSA provisioning for both paths.
(
    export FIREDASH_ETC="$TMP/etc"
    source "$ROOT/deploy/lib/secrets.sh"
    export FIREDASH_RELEASE="$TMP/release" FIREDASH_HOST=fire.example.test
    export ENV_FILE="$TMP/absent-env"
    effective_outbound_mail_https_proxy() { :; }
    validate_outbound_mail_https_proxy() { :; }
    generate_and_commit_secrets() { echo generate >> "$TRACE"; }
    validate_established_secrets() { echo preserve >> "$TRACE"; }
    render_env() { :; }
    ensure_report_transfer_keyring() { echo report-keyring >> "$TRACE"; }
    for FIREDASH_STATE in PRISTINE ESTABLISHED; do
        : > "$TRACE"
        secrets_ensure
        [[ $(tail -n1 "$TRACE") == report-keyring ]]
    done
)
echo 'ok: fresh and established standard secret initialization both ensure Report Transfer credentials'
