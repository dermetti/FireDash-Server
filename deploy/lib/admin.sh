#!/usr/bin/env bash
# Initial system administrator bootstrap. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

admin_setup_url_file=/root/firedash-initial-admin-setup-url

admin_state_snippet() {
    cat <<'PY'
from apps.authorization.models import SystemRole
roles = list(SystemRole.objects.filter(active=True).select_related("user"))
if any(r.user.is_active for r in roles):
    print("active")
elif len(roles) == 1:
    print("inactive")
elif len(roles) > 1:
    print("multiple")
else:
    print("none")
PY
}

# Return active|inactive|multiple|none for the current system administrator state.
system_admin_state() {
    local release=${FIREDASH_RELEASE:?} snippet
    snippet=$(mktemp)
    admin_state_snippet > "$snippet"
    (
        export DJANGO_SETTINGS_MODULE=config.settings.production
        load_env_file "$ENV_FILE"
        "$release/venv/bin/python" "$release/manage.py" shell < "$snippet"
    )
    local rc=$?
    rm -f "$snippet"
    return "$rc"
}

bootstrap_admin() {
    local release=${FIREDASH_RELEASE:?} base_url=${FIREDASH_BASE_URL:?} state url email name

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
            log "Initial administrator created."
            log "Setup URL stored at $admin_setup_url_file (expires in 24 hours)."
            ;;
        *)
            die "unexpected system administrator state: $state"
            ;;
    esac
}
