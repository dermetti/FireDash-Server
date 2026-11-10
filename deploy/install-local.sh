#!/usr/bin/env bash
# Stage-1 FireDash installer orchestrator. Runs from within the fetched checkout.
set -Eeuo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)

# shellcheck source=lib/common.sh
source "$SELF_DIR/lib/common.sh"
# shellcheck source=lib/state.sh
source "$SELF_DIR/lib/state.sh"
# shellcheck source=lib/nginx.sh
source "$SELF_DIR/lib/nginx.sh"
# shellcheck source=lib/postgresql.sh
source "$SELF_DIR/lib/postgresql.sh"
# shellcheck source=lib/systemd.sh
source "$SELF_DIR/lib/systemd.sh"
# shellcheck source=lib/admin.sh
source "$SELF_DIR/lib/admin.sh"

FIREDASH_REPO_ROOT=${FIREDASH_REPO_ROOT:-$ROOT}
export FIREDASH_REPO_ROOT

FIREDASH_PHASE=startup
FIREDASH_MAINTENANCE=0

on_error() {
    local rc=$?
    if (( BASH_SUBSHELL > 0 )); then
        # In a subshell: propagate the failure silently; the top-level trap reports once.
        exit "$rc"
    fi
    log_err "installer failed during phase '$FIREDASH_PHASE' (exit $rc)"
    if [[ $FIREDASH_MAINTENANCE == 1 ]]; then
        log_err "maintenance/quiesce had begun: application services may be stopped."
        log_err "Do not manually restart the old release if migrations may have run."
        log_err "Rerun this installer to converge."
    fi
    exit "$rc"
}
trap on_error ERR

# --- application-level CLI options (--ref is consumed by stage-0) ---
WAIT=0
while (($#)); do
    case "$1" in
        --verbose|--quiet)
            requested_mode=${1#--}
            [[ ${FIREDASH_OUTPUT_MODE:-default} == default || ${FIREDASH_OUTPUT_MODE} == "$requested_mode" ]] \
                || die "--verbose and --quiet are mutually exclusive"
            export FIREDASH_OUTPUT_MODE=$requested_mode
            shift ;;
        --wait) WAIT=1; shift ;;
        --base-url) (($# >= 2)) || die "--base-url requires a value"; FIREDASH_BASE_URL=$2; shift 2 ;;
        --base-url=*) FIREDASH_BASE_URL=${1#--base-url=}; shift ;;
        --tls-cert) (($# >= 2)) || die "--tls-cert requires a value"; FIREDASH_TLS_CERT_PATH=$2; shift 2 ;;
        --tls-cert=*) FIREDASH_TLS_CERT_PATH=${1#--tls-cert=}; shift ;;
        --tls-key) (($# >= 2)) || die "--tls-key requires a value"; FIREDASH_TLS_KEY_PATH=$2; shift 2 ;;
        --tls-key=*) FIREDASH_TLS_KEY_PATH=${1#--tls-key=}; shift ;;
        --admin-email) (($# >= 2)) || die "--admin-email requires a value"; FIREDASH_INITIAL_ADMIN_EMAIL=$2; shift 2 ;;
        --admin-email=*) FIREDASH_INITIAL_ADMIN_EMAIL=${1#--admin-email=}; shift ;;
        --admin-name) (($# >= 2)) || die "--admin-name requires a value"; FIREDASH_INITIAL_ADMIN_DISPLAY_NAME=$2; shift 2 ;;
        --admin-name=*) FIREDASH_INITIAL_ADMIN_DISPLAY_NAME=${1#--admin-name=}; shift ;;
        --outbound-mail-https-proxy) (($# >= 2)) || die "--outbound-mail-https-proxy requires a value"; FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY=$2; export FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY; shift 2 ;;
        --outbound-mail-https-proxy=*) FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY=${1#--outbound-mail-https-proxy=}; export FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY; shift ;;
        *) die "unknown installer argument" ;;
    esac
done

require_root
log "phase: startup"

# --- installer lock ---
exec 9>/run/lock/firedash-install.lock
if [[ $WAIT == 1 ]]; then
    flock 9
else
    if ! flock -n 9; then
        die "another FireDash installer is already running (lock: /run/lock/firedash-install.lock)"
    fi
fi

# Validate protected configuration before reading it or changing installation state.
command -v python3 >/dev/null || die "python3 is required for configuration validation"
python3 "$SELF_DIR/lib/configuration.py" check-files
CONFIG_SOURCE=$(python3 "$SELF_DIR/lib/configuration.py" install-source)

# --- load persisted non-secret configuration ---
if [[ -f $CONFIG_SOURCE ]]; then
    while IFS='=' read -r k v; do
        [[ -z ${k:-} || ${k:-} == \#* ]] && continue
        case "$k" in
            FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY)
                [[ ! ${FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY+x} ]] && export "$k=$v"
                ;;
            FIREDASH_BASE_URL|FIREDASH_TLS_CERT_PATH|FIREDASH_TLS_KEY_PATH|FIREDASH_REQUESTED_REF)
                [[ -z ${!k:-} ]] && export "$k=$v"
                ;;
        esac
    done < "$CONFIG_SOURCE"
fi

# Preserve a proxy configured by a release before install.conf owned this
# setting. An explicitly supplied (including empty) installer value wins.
if [[ ! ${FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY+x} && -f $ENV_FILE ]]; then
    FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY=$(env_value "$ENV_FILE" OUTBOUND_MAIL_HTTPS_PROXY)
    export FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY
fi

FIREDASH_STATE=$(classify_state)
export FIREDASH_STATE
log "installation state: $FIREDASH_STATE"

# --- prompt only for still-missing required values, via /dev/tty ---
prompt_for FIREDASH_BASE_URL "FireDash HTTPS base URL (e.g. https://firedash.example.org)"
prompt_for FIREDASH_TLS_CERT_PATH "TLS full-chain certificate path"
prompt_for FIREDASH_TLS_KEY_PATH "TLS private key path"

# Initial-admin email/name are prompted later, immediately before the admin phase,
# once actual administrator state is known (see lib/admin.sh).

# --- validate ---
FIREDASH_BASE_URL=$(normalize_base_url "$FIREDASH_BASE_URL")
FIREDASH_HOST=$(hostname_from_url "$FIREDASH_BASE_URL")
[[ -f $FIREDASH_TLS_CERT_PATH ]] || die "FIREDASH_TLS_CERT_PATH: certificate not found"
[[ -f $FIREDASH_TLS_KEY_PATH ]] || die "FIREDASH_TLS_KEY_PATH: private key not found"

FIREDASH_RESOLVED_SHA=${FIREDASH_RESOLVED_SHA:-}
[[ -n $FIREDASH_RESOLVED_SHA ]] || die "FIREDASH_RESOLVED_SHA is not set (run via deploy/install.sh)"
FIREDASH_REQUESTED_REF=${FIREDASH_REQUESTED_REF:-main}
FIREDASH_RELEASE=/srv/firedash/releases/$FIREDASH_RESOLVED_SHA

export FIREDASH_BASE_URL FIREDASH_HOST FIREDASH_TLS_CERT_PATH FIREDASH_TLS_KEY_PATH
export FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY
export FIREDASH_RESOLVED_SHA FIREDASH_REQUESTED_REF FIREDASH_RELEASE
export FIREDASH_INITIAL_ADMIN_EMAIL FIREDASH_INITIAL_ADMIN_DISPLAY_NAME

# --- stage non-secret configuration; preserve successful history until completion ---
persist_install_conf() {
    local tmp sha
    install -d -m 0750 -o root -g root /etc/fire-backend
    tmp=$(mktemp)
    cat > "$tmp" <<EOF
FIREDASH_INSTALL_SCHEMA_VERSION=2
FIREDASH_BASE_URL=$FIREDASH_BASE_URL
FIREDASH_TLS_CERT_PATH=$FIREDASH_TLS_CERT_PATH
FIREDASH_TLS_KEY_PATH=$FIREDASH_TLS_KEY_PATH
FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY=${FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY:-}
FIREDASH_REQUESTED_REF=$FIREDASH_REQUESTED_REF
EOF
    sha=$(env_value "$CONFIG_SOURCE" FIREDASH_LAST_SUCCESSFUL_SHA 2>/dev/null || true)
    if [[ -n $sha ]]; then
        printf 'FIREDASH_LAST_SUCCESSFUL_SHA=%s\n' "$sha" >> "$tmp"
    fi
    install_file_atomic "$tmp" "${INSTALL_CONF}.pending" 0640 root:root
    rm -f "$tmp"
}
# Validate the candidate before maintenance or replacing any live configuration.
validate_tls "$FIREDASH_TLS_CERT_PATH" "$FIREDASH_TLS_KEY_PATH" "$FIREDASH_HOST"
python3 "$SELF_DIR/lib/configuration.py" prepare
previous_sha=$(env_value "$CONFIG_SOURCE" FIREDASH_LAST_SUCCESSFUL_SHA 2>/dev/null || true)
if [[ $previous_sha == "$FIREDASH_RESOLVED_SHA" ]]; then
    log_complete "No newer release available; converging configuration for resolved SHA $FIREDASH_RESOLVED_SHA."
fi
log_verbose "configuration validated; protected selectors and managed secrets preserved"

# ============================== phases ==============================

FIREDASH_PHASE=bootstrap
log "phase: bootstrap"
run_step "$ROOT/deploy/bootstrap-lxc.sh"

FIREDASH_PHASE=release-build
log "phase: release-build"
run_step "$ROOT/deploy/deploy-release.sh" build

# Close all report writers before proving missing-key initial enablement safe.
# Validate mail transport using the built release before stopping writers.
python3 "$SELF_DIR/lib/configuration.py" prepare
source "$SELF_DIR/lib/secrets.sh"
validate_outbound_mail_https_proxy "$FIREDASH_RELEASE" "$(effective_outbound_mail_https_proxy)" \
    || die "OUTBOUND_MAIL_HTTPS_PROXY is invalid"
if [[ $FIREDASH_STATE == ESTABLISHED ]]; then
    validate_premaintenance_secrets "$FIREDASH_RELEASE"
fi
persist_install_conf
FIREDASH_PHASE=quiesce
log "phase: quiesce"
FIREDASH_MAINTENANCE=1
run_step quiesce

FIREDASH_PHASE=secrets
log "phase: secrets"
run_step "$ROOT/deploy/initialize-firedash.sh" secrets
if [[ ! -e $FIREDASH_ETC/overrides.env ]]; then
    overrides_tmp=$(mktemp)
    printf '# Optional allowlisted application tuning. Edit, then rerun the installer.\n' > "$overrides_tmp"
    install_file_atomic "$overrides_tmp" "$FIREDASH_ETC/overrides.env" 0640 root:root
    rm -f "$overrides_tmp"
fi
install_file_atomic "${INSTALL_CONF}.pending" "$INSTALL_CONF" 0640 root:root
rm -f "${INSTALL_CONF}.pending"

FIREDASH_PHASE=postgres-bootstrap
log "phase: postgres-bootstrap"
run_step postgres_bootstrap "$ROOT/deploy/postgresql/bootstrap-production.sql"

FIREDASH_PHASE=nginx
log "phase: nginx"
run_step install_nginx

FIREDASH_PHASE=activate
log "phase: activate"
run_step "$ROOT/deploy/deploy-release.sh" activate

FIREDASH_PHASE=start-socket
log "phase: start-socket"
run_step activate_socket

FIREDASH_PHASE=health
log "phase: health"
health_probe() {
    local host=$FIREDASH_HOST i
    log "waiting for application liveness"
    for i in $(seq 1 30); do
        if curl -fsS --resolve "$host:443:127.0.0.1" "https://$host/health/live" >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    curl -fsS --resolve "$host:443:127.0.0.1" "https://$host/health/live" >/dev/null \
        || die "application liveness check failed"
    log "application is live; verifying readiness"
    for i in $(seq 1 15); do
        if curl -fsS --resolve "$host:443:127.0.0.1" "https://$host/health/ready" >/dev/null 2>&1; then
            log "application is ready"
            return 0
        fi
        sleep 2
    done
    die "application readiness check failed"
}
health_probe

FIREDASH_PHASE=timers
log "phase: timers"
run_step activate_timers

FIREDASH_PHASE=admin
log "phase: admin"
run_step "$ROOT/deploy/initialize-firedash.sh" admin

FIREDASH_PHASE=verify
log "phase: verify"
run_step "$ROOT/deploy/verify-deployment.sh"

FIREDASH_PHASE=finalize
log "phase: finalize"
record_success() {
    local tmp sha=$FIREDASH_RESOLVED_SHA
    tmp=$(mktemp)
    if [[ -f $INSTALL_CONF ]]; then
        grep -v '^FIREDASH_LAST_SUCCESSFUL_SHA=' "$INSTALL_CONF" > "$tmp" || true
    else
        : > "$tmp"
    fi
    printf 'FIREDASH_LAST_SUCCESSFUL_SHA=%s\n' "$sha" >> "$tmp"
    install_file_atomic "$tmp" "$INSTALL_CONF" 0640 root:root
    rm -f "$tmp"
    log "deployment successful; active SHA: $sha"
}
record_success

log_complete "FireDash installation complete. Resolved SHA: $FIREDASH_RESOLVED_SHA; successfully deployed SHA: $FIREDASH_RESOLVED_SHA."

display_admin_setup_url
