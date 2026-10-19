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
    local timer svc
    for timer in fire-backup.timer fire-publication-worker.timer fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer; do
        systemctl stop "$timer" || die "could not stop $timer; cannot proceed with migration"
    done
    # Close activation sources before stopping workers: a queued wake must not
    # restart the build service while migrations are running.
    systemctl stop fire-publication-build.socket || die "could not stop publication build socket"
    systemctl stop fire-backend.socket || die "could not stop backend socket"
    for svc in fire-backup.service fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
        systemctl stop "$svc" || die "could not stop $svc; cannot proceed with migration"
    done
    systemctl stop fire-backend.service || die "could not stop backend service"

    for svc in fire-backup.timer fire-backup.service fire-publication-worker.timer fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer fire-publication-build.socket fire-backend.socket fire-backend.service fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
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
    systemctl disable --now fire-publication-worker.timer || return
    systemctl stop fire-publication-worker.service || return
    for timer in fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer; do
        systemctl enable --now "$timer"
    done
    systemctl enable --now fire-publication-delivery.service
    if [[ -f /etc/fire-backend/backup.env ]] \
        && [[ -f $SECRET_DIR/backup-pgpass ]] \
        && [[ -f $SECRET_DIR/restic-password ]]; then
        systemctl enable --now fire-backup.timer
    fi
}
