#!/usr/bin/env bash
# Deployment-artifact convergence: remove FireDash-owned artifacts that older
# releases installed but the current release no longer uses. Source this file; do
# not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

# Overridable for tests. Units are always removed from this exact directory only.
FIREDASH_SYSTEMD_UNIT_DIR=${FIREDASH_SYSTEMD_UNIT_DIR:-/etc/systemd/system}

# Explicit fixed allowlist of obsolete FireDash-owned files. Exact absolute paths
# only; no globs, no recursion, and never derived from the current release.
OBSOLETE_FILES=(
    /etc/sudoers.d/fire-pdf-sanitizer
    /usr/local/lib/fire-backend/fire-pdf-sanitize
)

# Explicit fixed allowlist of obsolete FireDash-owned systemd units. Entries are
# removed from FIREDASH_SYSTEMD_UNIT_DIR only. Add future obsolete units here.
OBSOLETE_UNITS=()

# Set to 1 by remove_obsolete_unit when it actually removes a unit file, so the
# top-level step can issue a single daemon-reload afterward.
_OBSOLETE_UNIT_REMOVED=0

# Remove exactly one obsolete file (or symlink). Idempotent: a missing path is a
# success. A directory is refused and reported as a failure; any removal error is
# a hard failure.
remove_obsolete_file() {
    local path=$1
    if [[ $path != /* ]]; then
        log_err "refusing non-absolute obsolete path: $path"
        return 1
    fi
    if [[ ! -e $path && ! -L $path ]]; then
        log "obsolete artifact already absent: $path"
        return 0
    fi
    if [[ -d $path && ! -L $path ]]; then
        log_err "refusing to remove directory (not a file): $path"
        return 1
    fi
    if ! rm -f -- "$path"; then
        log_err "failed to remove obsolete artifact: $path"
        return 1
    fi
    log "removed obsolete artifact: $path"
    return 0
}

# Disable/stop and remove exactly one obsolete systemd unit. Idempotent and safe
# on repeated runs. Failure to stop, disable, or remove the declared unit is a
# hard failure. Only the exact FireDash-owned unit file under
# FIREDASH_SYSTEMD_UNIT_DIR is removed; the caller reloads the daemon once.
remove_obsolete_unit() {
    local unit=$1
    if [[ ! $unit =~ ^[A-Za-z0-9@._:-]+\.(service|socket|timer|target)$ ]]; then
        log_err "refusing invalid obsolete unit name: $unit"
        return 1
    fi
    local unit_file="$FIREDASH_SYSTEMD_UNIT_DIR/$unit"
    if [[ ! -e $unit_file && ! -L $unit_file ]]; then
        log "obsolete unit already absent: $unit"
        return 0
    fi
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            if ! systemctl stop "$unit" >/dev/null 2>&1; then
                log_err "failed to stop obsolete unit: $unit"
                return 1
            fi
        fi
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            if ! systemctl disable "$unit" >/dev/null 2>&1; then
                log_err "failed to disable obsolete unit: $unit"
                return 1
            fi
        fi
    fi
    if ! rm -f -- "$unit_file"; then
        log_err "failed to remove obsolete unit file: $unit"
        return 1
    fi
    log "removed obsolete unit file: $unit"
    _OBSOLETE_UNIT_REMOVED=1
    return 0
}

# Converge the host by removing every currently-registered obsolete artifact.
# Returns non-zero if any declared artifact could not be removed; otherwise 0.
remove_obsolete_deployment_artifacts() {
    _OBSOLETE_UNIT_REMOVED=0
    local path unit status=0
    for path in "${OBSOLETE_FILES[@]}"; do
        remove_obsolete_file "$path" || status=1
    done
    for unit in "${OBSOLETE_UNITS[@]}"; do
        remove_obsolete_unit "$unit" || status=1
    done
    if (( _OBSOLETE_UNIT_REMOVED )); then
        if command -v systemctl >/dev/null 2>&1; then
            if ! systemctl daemon-reload; then
                log_err "failed to reload systemd after obsolete unit removal"
                status=1
            else
                log "systemd daemon reloaded after obsolete unit removal"
            fi
        fi
    fi
    return "$status"
}
