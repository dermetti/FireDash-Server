#!/usr/bin/env bash
# systemd unit installation, quiescing, and activation. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

install_systemd_units() {
    local src=${FIREDASH_REPO_ROOT:?}/deploy/systemd unit
    reconcile_report_transfer_dropins
    for unit in "$src"/*.service "$src"/*.socket "$src"/*.timer; do
        install -m 0644 -o root -g root "$unit" /etc/systemd/system/"$(basename "$unit")"
    done
    systemctl daemon-reload
}

# Retire only the exact former documented drop-in. Preserve unrelated overrides.
reconcile_report_transfer_dropins() {
    local base=${FIREDASH_SYSTEMD_UNIT_DIR:-/etc/systemd/system} file content
    for file in "$base/fire-backend.service.d/"*.conf /run/systemd/system/fire-backend.service.d/*.conf; do
        [[ -e $file || -L $file ]] || continue
        if grep -Eq 'report-transfer|firedash-reports' "$file"; then
            [[ ! -L $file && -f $file ]] || die "unsafe Report Transfer override: $file"
            content=$(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d; s/\r$//' "$file")
            if [[ $content == $'[Service]\nLoadCredential=report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json\nReadWritePaths=/var/spool/firedash-reports' ]]; then
                rm -- "$file" || die "could not retire obsolete Report Transfer drop-in: $file"
            else
                die "conflicting Report Transfer override at $file; reconcile it with deploy/systemd/fire-backend.service, retaining unrelated administrator settings, then rerun"
            fi
        fi
    done
}

# Stop every service that could touch the DB, including the activation socket.
quiesce() {
    log "entering maintenance/quiesce mode"
    local timer svc
    for timer in fire-backup.timer fire-publication-worker.timer fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer fire-report-expiry.timer; do
        systemctl stop "$timer" || die "could not stop $timer; cannot proceed with migration"
    done
    # Close activation sources before stopping workers: a queued wake must not
    # restart the build service while migrations are running.
    systemctl stop fire-publication-build.socket || die "could not stop publication build socket"
    systemctl stop fire-backend.socket || die "could not stop backend socket"
    for svc in fire-backup.service fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service fire-report-expiry.service; do
        systemctl stop "$svc" || die "could not stop $svc; cannot proceed with migration"
    done
    systemctl stop fire-backend.service || die "could not stop backend service"

    for svc in fire-backup.timer fire-backup.service fire-publication-worker.timer fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer fire-report-expiry.timer fire-report-expiry.service fire-publication-build.socket fire-backend.socket fire-backend.service fire-publication-worker.service fire-publication-delivery.service fire-publication-build.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
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
    for timer in fire-publication-build.timer fire-publication-maintenance.timer fire-import-staging-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer fire-report-expiry.timer; do
        systemctl enable --now "$timer" || return 1
    done
    systemctl enable --now fire-publication-delivery.service || return 1
    if [[ -f /etc/fire-backend/backup.env ]] \
        && [[ -f $SECRET_DIR/backup-pgpass ]] \
        && [[ -f $SECRET_DIR/restic-password ]]; then
        systemctl enable --now fire-backup.timer || return 1
    fi
    return 0
}
