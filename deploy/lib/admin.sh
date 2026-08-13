#!/usr/bin/env bash
# Initial system administrator bootstrap. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

admin_setup_url_file=${ADMIN_SETUP_URL_FILE:-/root/firedash-initial-admin-setup-url}
admin_created_marker=${ADMIN_CREATED_MARKER:-/run/firedash-admin-created}

admin_state_snippet() {
    cat <<'PY'
import django

django.setup()

from apps.authorization.models import SystemRole
from apps.authorization.services import classify_system_admin_state

roles = list(SystemRole.objects.filter(active=True).select_related("user"))
print(classify_system_admin_state(roles))
PY
}

# Return active|inactive|multiple|none for the current system administrator state.
# Runs a quiet python snippet (not `manage.py shell`, which emits an automatic-import
# banner to stdout), so stdout contains exactly one token.
system_admin_state() {
    local release=${FIREDASH_RELEASE:?} snippet
    snippet=$(mktemp)
    admin_state_snippet > "$snippet"
    (
        export DJANGO_SETTINGS_MODULE=config.settings.production
        export PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}"
        load_env_file "$ENV_FILE"
        "$release/venv/bin/python" "$snippet"
    )
    local rc=$?
    rm -f "$snippet"
    return "$rc"
}

bootstrap_admin() {
    local release=${FIREDASH_RELEASE:?} base_url=${FIREDASH_BASE_URL:?} state url email name

    rm -f "$admin_created_marker"
    state=$(system_admin_state)
    case "$state" in
        active)
            log "system administrator already active; skipping bootstrap"
            ;;
        inactive)
            log "one inactive bootstrap administrator exists; not creating another"
            if [[ ! -f $admin_setup_url_file ]]; then
                log_warn "setup URL file is missing; run:"
                log_warn "  $release/venv/bin/python $release/manage.py reissue_system_admin_setup --base-url $base_url"
            fi
            ;;
        multiple)
            die "multiple/inconsistent system administrators exist; refusing to initialize"
            ;;
        none)
            prompt_for FIREDASH_INITIAL_ADMIN_EMAIL "Initial system administrator email"
            prompt_for FIREDASH_INITIAL_ADMIN_DISPLAY_NAME "Initial system administrator display name"
            email=${FIREDASH_INITIAL_ADMIN_EMAIL:?}
            name=${FIREDASH_INITIAL_ADMIN_DISPLAY_NAME:?}
            url=$( (
                export DJANGO_SETTINGS_MODULE=config.settings.production
                load_env_file "$ENV_FILE"
                "$release/venv/bin/python" "$release/manage.py" bootstrap_system_admin \
                    --email "$email" --display-name "$name" --base-url "$base_url"
            ) )
            install -m 0600 -o root -g root /dev/null "$admin_setup_url_file"
            printf '%s\n' "$url" > "$admin_setup_url_file"
            chmod 600 "$admin_setup_url_file"
            chown root:root "$admin_setup_url_file"
            # Record that a NEW administrator was created THIS run (no raw token here).
            printf 'created=1\n' > "$admin_created_marker"
            chmod 600 "$admin_created_marker"
            chown root:root "$admin_created_marker"
            log "Initial administrator created."
            log "Setup URL stored at $admin_setup_url_file (expires in 24 hours)."
            ;;
        *)
            die "unexpected system administrator state: $state"
            ;;
    esac
}

# Display the initial admin setup URL at the end of a successful install, only if a
# NEW administrator was created during this run. Prints to /dev/tty (never logs).
display_admin_setup_url() {
    [[ -f $admin_created_marker ]] || return 0
    if [[ $(read_secret "$admin_created_marker") != created=1 ]]; then
        rm -f "$admin_created_marker"
        return 0
    fi
    local url
    url=$(read_secret "$admin_setup_url_file" 2>/dev/null || true)
    if [[ -n $url && -e /dev/tty && -w /dev/tty ]]; then
        (
            exec 2>/dev/null
            printf '\nInitial System Administrator setup URL\nValid for 24 hours:\n\n%s\n\nA copy is stored at:\n%s\n' \
                "$url" "$admin_setup_url_file" > /dev/tty
        ) || true
    else
        log "Initial administrator setup URL stored at $admin_setup_url_file"
    fi
    rm -f "$admin_created_marker"
}
