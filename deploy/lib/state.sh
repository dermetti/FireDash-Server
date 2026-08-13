#!/usr/bin/env bash
# Installation state classification: PRISTINE | BOOTSTRAP_INCOMPLETE | ESTABLISHED.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

# Echo present|absent|unknown for FireDash database presence.
db_firedash_state() {
    command -v psql >/dev/null 2>&1 || { echo unknown; return; }
    command -v runuser >/dev/null 2>&1 || { echo unknown; return; }
    if ! as_postgres psql -tAc "SELECT 1" >/dev/null 2>&1; then
        echo unknown
        return
    fi
    local out
    out=$(as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='fire_backend'" 2>/dev/null || true)
    [[ $out == 1 ]] && { echo present; return; }
    out=$(as_postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname IN ('database_owner','application_runtime','backup_role') LIMIT 1" 2>/dev/null || true)
    [[ $out == 1 ]] && { echo present; return; }
    echo absent
}

classify_state() {
    # Completed secret set or completed install are authoritative.
    [[ -e $SECRETS_MARKER ]] && { echo ESTABLISHED; return; }
    [[ -L $CURRENT_LINK ]] && { echo ESTABLISHED; return; }

    if [[ -f $INSTALL_CONF ]]; then
        local sha
        sha=$(env_value "$INSTALL_CONF" FIREDASH_LAST_SUCCESSFUL_SHA)
        [[ -n $sha ]] && { echo ESTABLISHED; return; }
    fi

    local dbstate
    dbstate=$(db_firedash_state)
    if [[ $dbstate == present ]]; then
        echo ESTABLISHED
        return
    fi
    if [[ $dbstate == unknown && -f $ENV_FILE && -n $(env_value "$ENV_FILE" POSTGRES_PASSWORD) ]]; then
        # Cannot prove the database is absent; be conservative and fail closed.
        echo ESTABLISHED
        return
    fi

    # BOOTSTRAP_INCOMPLETE: installer artifacts exist but no authoritative state depends on them.
    [[ -f $INSTALL_CONF ]] && { echo BOOTSTRAP_INCOMPLETE; return; }
    [[ -f $ENV_FILE ]] && { echo BOOTSTRAP_INCOMPLETE; return; }
    if [[ -d $SECRET_DIR ]] && compgen -G "$SECRET_DIR/*" >/dev/null 2>&1; then
        echo BOOTSTRAP_INCOMPLETE
        return
    fi
    if [[ -d $RELEASES_DIR ]] && compgen -G "$RELEASES_DIR/*" >/dev/null 2>&1; then
        echo BOOTSTRAP_INCOMPLETE
        return
    fi

    echo PRISTINE
}
