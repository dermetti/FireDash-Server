#!/usr/bin/env bash
# systemd unit installation, quiescing, and activation. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

install_systemd_units() {
    local src=${FIREDASH_REPO_ROOT:?}/deploy/systemd unit
    for unit in "$src"/*.service "$src"/*.socket "$src"/*.timer; do
        install -m 0644 -o root -g root "$unit" /etc/systemd/system/"$(basename "$unit")"
    done
    systemctl daemon-reload
}

# Stop every service that could touch the DB, including the activation socket.
quiesce() {
    log "entering maintenance/quiesce mode"
    if systemctl list-unit-files fire-backup.timer >/dev/null 2>&1 \
        && systemctl is-enabled --quiet fire-backup.timer 2>/dev/null; then
        systemctl stop fire-backup.timer 2>/dev/null || true
        systemctl stop fire-backup.service 2>/dev/null || true
    fi
    local timer svc
    for timer in fire-publication-worker.timer fire-publication-build.timer fire-publication-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer; do
        systemctl stop "$timer" 2>/dev/null || true
    done
    for svc in fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
        systemctl stop "$svc" 2>/dev/null || true
    done
    systemctl stop fire-backend.socket 2>/dev/null || true
    systemctl stop fire-backend.service 2>/dev/null || true

    for svc in fire-backend.socket fire-backend.service fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
        if systemctl is-active --quiet "$svc"; then
            die "unit $svc is still active; cannot proceed with migration"
        fi
    done
    log "application services quiesced"
}

activate_socket() {
    log "enabling socket activation"
    systemctl enable --now fire-backend.socket
    systemctl enable --now fire-pdf-sanitizer-broker.socket
    systemctl enable --now fire-publication-build.socket
}

activate_timers() {
    local timer
    systemctl disable --now fire-publication-worker.timer 2>/dev/null || true
    systemctl stop fire-publication-worker.service 2>/dev/null || true
    for timer in fire-publication-build.timer fire-publication-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer; do
        systemctl enable --now "$timer"
    done
    systemctl enable --now fire-publication-delivery.service
    if [[ -f /etc/fire-backend/backup.env ]] \
        && [[ -f $SECRET_DIR/backup-pgpass ]] \
        && [[ -f $SECRET_DIR/restic-password ]]; then
        systemctl enable --now fire-backup.timer
    fi
}
