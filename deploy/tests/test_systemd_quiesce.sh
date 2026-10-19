#!/usr/bin/env bash
# Model systemctl state to verify helper ordering/failure handling, not systemd integration.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export TRACE="$TMP/trace" STUCK="" REMAINS="" FIXTURE_STATE="$TMP/state"
source "$ROOT/deploy/lib/systemd.sh"

systemctl() {
    printf '%s\n' "$*" >> "$TRACE"
    case "$1" in
        stop)
            [[ $2 != "$STUCK" ]] || return 1
            [[ $2 != "$REMAINS" ]] || return 0
            rm -f "$FIXTURE_STATE/$2"
            ;;
        is-active) [[ -e "$FIXTURE_STATE/${@: -1}" ]] ;;
        disable) [[ "$STUCK" != legacy-disable ]] ;;
        *) return 0 ;;
    esac
}
setup_case() {
    mkdir -p "$FIXTURE_STATE"
    : > "$TRACE"
    for unit in fire-publication-build.socket fire-publication-build.service fire-backup.service fire-backup.timer fire-stale-installation.timer; do
        touch "$FIXTURE_STATE/$unit"
    done
}
setup_case
quiesce
[[ -z $(find "$FIXTURE_STATE" -type f -print) ]]
socket_line=$(grep -n '^stop fire-publication-build.socket$' "$TRACE" | cut -d: -f1)
worker_line=$(grep -n '^stop fire-publication-build.service$' "$TRACE" | cut -d: -f1)
[[ $socket_line -lt $worker_line ]]
quiesce # interrupted/repeated deployment convergence

for STUCK in fire-publication-build.socket fire-publication-build.service fire-backup.service fire-backup.timer fire-stale-installation.timer; do
    setup_case
    if (quiesce); then echo "FAIL: quiesce accepted active $STUCK" >&2; exit 1; fi
    rm -f "$FIXTURE_STATE/$STUCK"
done
STUCK=""
REMAINS=fire-publication-build.socket
setup_case
if (quiesce); then echo 'FAIL: quiesce accepted a still-active socket' >&2; exit 1; fi
REMAINS=""
STUCK=legacy-disable
if (activate_timers); then echo 'FAIL: obsolete timer disable failure ignored' >&2; exit 1; fi
STUCK=fire-publication-worker.service
if (activate_timers); then echo 'FAIL: obsolete worker stop failure ignored' >&2; exit 1; fi
echo 'ok: activation sources close before workers; active units and legacy retirement failures block deployment'
