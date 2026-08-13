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

require_root

# --- application-level CLI options (--ref is consumed by stage-0) ---
WAIT=0
while (($#)); do
    case "$1" in
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
        *) die "unknown argument: $1" ;;
    esac
done

# --- installer lock ---
exec 9>/run/lock/firedash-install.lock
if [[ $WAIT == 1 ]]; then
    flock 9
else
    if ! flock -n 9; then
        die "another FireDash installer is already running (lock: /run/lock/firedash-install.lock)"
    fi
fi

# --- load persisted non-secret configuration ---
if [[ -f $INSTALL_CONF ]]; then
    while IFS='=' read -r k v; do
        [[ -z ${k:-} || ${k:-} == \#* ]] && continue
        case "$k" in
            FIREDASH_BASE_URL|FIREDASH_TLS_CERT_PATH|FIREDASH_TLS_KEY_PATH|FIREDASH_REQUESTED_REF)
                [[ -z ${!k:-} ]] && export "$k=$v"
                ;;
        esac
    done < "$INSTALL_CONF"
fi

FIREDASH_STATE=$(classify_state)
export FIREDASH_STATE
log "installation state: $FIREDASH_STATE"

# --- prompt only for still-missing required values, via /dev/tty ---
prompt_for FIREDASH_BASE_URL "FireDash HTTPS base URL (e.g. https://firedash.mjblab.de)"
prompt_for FIREDASH_TLS_CERT_PATH "TLS full-chain certificate path"
prompt_for FIREDASH_TLS_KEY_PATH "TLS private key path"

# Initial-admin email/name are prompted later, immediately before the admin phase,
# once actual administrator state is known (see lib/admin.sh).

# --- validate ---
FIREDASH_BASE_URL=$(normalize_base_url "$FIREDASH_BASE_URL")
FIREDASH_HOST=$(hostname_from_url "$FIREDASH_BASE_URL")
[[ -f $FIREDASH_TLS_CERT_PATH ]] || die "TLS certificate not found: $FIREDASH_TLS_CERT_PATH"
[[ -f $FIREDASH_TLS_KEY_PATH ]] || die "TLS private key not found: $FIREDASH_TLS_KEY_PATH"

FIREDASH_RESOLVED_SHA=${FIREDASH_RESOLVED_SHA:-}
[[ -n $FIREDASH_RESOLVED_SHA ]] || die "FIREDASH_RESOLVED_SHA is not set (run via deploy/install.sh)"
FIREDASH_REQUESTED_REF=${FIREDASH_REQUESTED_REF:-main}
FIREDASH_RELEASE=/srv/firedash/releases/$FIREDASH_RESOLVED_SHA

export FIREDASH_BASE_URL FIREDASH_HOST FIREDASH_TLS_CERT_PATH FIREDASH_TLS_KEY_PATH
export FIREDASH_RESOLVED_SHA FIREDASH_REQUESTED_REF FIREDASH_RELEASE
export FIREDASH_INITIAL_ADMIN_EMAIL FIREDASH_INITIAL_ADMIN_DISPLAY_NAME

# --- persist non-secret configuration (before any destructive work) ---
persist_install_conf() {
    local tmp sha
    install -d -m 0750 -o root -g root /etc/fire-backend
    tmp=$(mktemp)
    cat > "$tmp" <<EOF
FIREDASH_INSTALL_SCHEMA_VERSION=1
FIREDASH_BASE_URL=$FIREDASH_BASE_URL
FIREDASH_TLS_CERT_PATH=$FIREDASH_TLS_CERT_PATH
FIREDASH_TLS_KEY_PATH=$FIREDASH_TLS_KEY_PATH
FIREDASH_REQUESTED_REF=$FIREDASH_REQUESTED_REF
EOF
    sha=$(env_value "$INSTALL_CONF" FIREDASH_LAST_SUCCESSFUL_SHA 2>/dev/null || true)
    if [[ -n $sha ]]; then
        printf 'FIREDASH_LAST_SUCCESSFUL_SHA=%s\n' "$sha" >> "$tmp"
    fi
    install_file_atomic "$tmp" "$INSTALL_CONF" 0640 root:root
    rm -f "$tmp"
}
persist_install_conf

# ============================== phases ==============================

FIREDASH_PHASE=bootstrap
"$ROOT/deploy/bootstrap-lxc.sh"

FIREDASH_PHASE=release-build
"$ROOT/deploy/deploy-release.sh" build

FIREDASH_PHASE=secrets
"$ROOT/deploy/initialize-firedash.sh" secrets

FIREDASH_PHASE=postgres-bootstrap
postgres_bootstrap "$ROOT/deploy/postgresql/bootstrap-production.sql"

FIREDASH_PHASE=nginx
install_nginx

FIREDASH_PHASE=quiesce
FIREDASH_MAINTENANCE=1
quiesce

FIREDASH_PHASE=activate
"$ROOT/deploy/deploy-release.sh" activate

FIREDASH_PHASE=start-socket
activate_socket

FIREDASH_PHASE=health
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
        || die "application liveness check failed for https://$host/health/live"
    log "application is live; verifying readiness"
    for i in $(seq 1 15); do
        if curl -fsS --resolve "$host:443:127.0.0.1" "https://$host/health/ready" >/dev/null 2>&1; then
            log "application is ready"
            return 0
        fi
        sleep 2
    done
    die "application readiness check failed for https://$host/health/ready"
}
health_probe

FIREDASH_PHASE=timers
activate_timers

FIREDASH_PHASE=admin
"$ROOT/deploy/initialize-firedash.sh" admin

FIREDASH_PHASE=verify
"$ROOT/deploy/verify-deployment.sh"

FIREDASH_PHASE=finalize
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

log "FireDash installation complete."

display_admin_setup_url
