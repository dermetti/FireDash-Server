#!/usr/bin/env bash
# Regression test for the deployment-artifact convergence helper:
# - obsolete files are removed idempotently;
# - non-registered paths are never touched;
# - directories are refused and reported as a failure;
# - obsolete units are stopped/disabled, removed, and trigger one daemon-reload;
# - removal/stop/disable failures cause a non-zero convergence status.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB_DIR=$(CDPATH= cd -- "$SELF_DIR/../lib" && pwd)

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

export FIREDASH_SYSTEMD_UNIT_DIR="$TMPROOT/units"
mkdir -p "$FIREDASH_SYSTEMD_UNIT_DIR"

BIN="$TMPROOT/bin"
mkdir -p "$BIN"
export SYSTEMCTL_LOG="$TMPROOT/systemctl.log"
cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
case "$1" in
    is-active) exit "${SYSTEMCTL_IS_ACTIVE_RC:-0}" ;;
    is-enabled) exit "${SYSTEMCTL_IS_ENABLED_RC:-0}" ;;
    stop) exit "${SYSTEMCTL_STOP_RC:-0}" ;;
    disable) exit "${SYSTEMCTL_DISABLE_RC:-0}" ;;
    daemon-reload) exit "${SYSTEMCTL_RELOAD_RC:-0}" ;;
    *) exit 0 ;;
esac
EOF
chmod +x "$BIN/systemctl"
export PATH="$BIN:$PATH"

# shellcheck source=../lib/common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=../lib/converge.sh
source "$LIB_DIR/converge.sh"

failures=0
assert_absent() {
    if [[ ! -e $1 && ! -L $1 ]]; then
        echo "ok: $2"
    else
        echo "FAIL: $2 (still present: $1)" >&2
        failures=$((failures + 1))
    fi
}
assert_present() {
    if [[ -e $1 || -L $1 ]]; then
        echo "ok: $2"
    else
        echo "FAIL: $2 (missing: $1)" >&2
        failures=$((failures + 1))
    fi
}
assert_rc_eq() {
    local expected=$1 actual=$2 label=$3
    if [[ $expected -eq $actual ]]; then
        echo "ok: $label"
    else
        echo "FAIL: $label (rc=$actual, expected $expected)" >&2
        failures=$((failures + 1))
    fi
}
reset_systemctl_rcs() {
    unset SYSTEMCTL_IS_ACTIVE_RC SYSTEMCTL_IS_ENABLED_RC \
        SYSTEMCTL_STOP_RC SYSTEMCTL_DISABLE_RC SYSTEMCTL_RELOAD_RC
}

# 1. Obsolete file present -> removed, success.
OBSOLETE_FILES=("$TMPROOT/old-file")
OBSOLETE_UNITS=()
printf 'x\n' > "$TMPROOT/old-file"
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_absent "$TMPROOT/old-file" "obsolete file removed"
assert_rc_eq 0 "$rc" "obsolete file removal returns success"

# 2. Absent -> rerun succeeds (idempotent).
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_rc_eq 0 "$rc" "absent obsolete file rerun succeeds"

# 3. Exact-path safety: a non-registered sibling is preserved.
printf 'y\n' > "$TMPROOT/keep-me"
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
assert_present "$TMPROOT/keep-me" "non-registered file preserved"

# 4. Directory refused -> hard failure, directory preserved.
mkdir -p "$TMPROOT/old-dir"
OBSOLETE_FILES=("$TMPROOT/old-dir")
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_present "$TMPROOT/old-dir" "directory refused (not removed)"
assert_rc_eq 1 "$rc" "directory refusal returns failure"

# 5. Obsolete unit: stop + disable, remove file, and daemon-reload once.
reset_systemctl_rcs
: > "$SYSTEMCTL_LOG"
OBSOLETE_FILES=()
OBSOLETE_UNITS=("obsolete-helper.service")
printf 'x\n' > "$FIREDASH_SYSTEMD_UNIT_DIR/obsolete-helper.service"
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_rc_eq 0 "$rc" "obsolete unit removal returns success"
assert_absent "$FIREDASH_SYSTEMD_UNIT_DIR/obsolete-helper.service" "obsolete unit file removed"
if grep -q 'stop obsolete-helper.service' "$SYSTEMCTL_LOG"; then
    echo "ok: obsolete unit stopped"
else
    echo "FAIL: obsolete unit was not stopped" >&2
    failures=$((failures + 1))
fi
if grep -q 'disable obsolete-helper.service' "$SYSTEMCTL_LOG"; then
    echo "ok: obsolete unit disabled"
else
    echo "FAIL: obsolete unit was not disabled" >&2
    failures=$((failures + 1))
fi
if grep -q '^daemon-reload$' "$SYSTEMCTL_LOG"; then
    echo "ok: daemon-reload issued after unit removal"
else
    echo "FAIL: daemon-reload not issued after unit removal" >&2
    failures=$((failures + 1))
fi

# 6. Rerun with unit already absent -> no redundant daemon-reload.
: > "$SYSTEMCTL_LOG"
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_rc_eq 0 "$rc" "absent unit rerun succeeds"
if grep -q '^daemon-reload$' "$SYSTEMCTL_LOG"; then
    echo "FAIL: redundant daemon-reload on rerun" >&2
    failures=$((failures + 1))
else
    echo "ok: no redundant daemon-reload on rerun"
fi

# 7. Stop failure -> hard failure, unit file preserved.
reset_systemctl_rcs
export SYSTEMCTL_IS_ACTIVE_RC=0 SYSTEMCTL_STOP_RC=1
: > "$SYSTEMCTL_LOG"
OBSOLETE_UNITS=("broken-helper.service")
printf 'x\n' > "$FIREDASH_SYSTEMD_UNIT_DIR/broken-helper.service"
remove_obsolete_deployment_artifacts >/dev/null 2>/dev/null
rc=$?
assert_rc_eq 1 "$rc" "stop failure returns failure"
assert_present "$FIREDASH_SYSTEMD_UNIT_DIR/broken-helper.service" "unit file preserved on stop failure"

if [[ $failures -eq 0 ]]; then
    echo "convergence tests passed"
else
    echo "convergence tests FAILED ($failures)" >&2
    exit 1
fi
