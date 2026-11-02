#!/usr/bin/env bash
# Private spool provisioning, shared by bootstrap and the verifier.
_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

REPORT_SPOOL=${FIREDASH_REPORT_SPOOL:-/var/spool/firedash-reports}

validate_report_spool() {
    [[ ! -L $REPORT_SPOOL && -d $REPORT_SPOOL ]] \
        && [[ $(readlink -f "$REPORT_SPOOL") == "$REPORT_SPOOL" ]] \
        && [[ $(stat -c '%U:%G:%a' "$REPORT_SPOOL") == fire_backend:fire_backend:700 ]]
}

ensure_report_spool() {
    if [[ -e $REPORT_SPOOL || -L $REPORT_SPOOL ]]; then
        validate_report_spool || die "unsafe report spool $REPORT_SPOOL; restore a real fire_backend:fire_backend directory with mode 0700 (ciphertext and receipts must be preserved)"
    else
        [[ $(readlink -f "$(dirname "$REPORT_SPOOL")") == "$(dirname "$REPORT_SPOOL")" ]] \
            || die "report spool parent must not contain symlinks"
        install -d -o fire_backend -g fire_backend -m 0700 "$REPORT_SPOOL" \
            || die "could not create private report spool"
        validate_report_spool || die "report spool provisioning failed"
    fi
}
