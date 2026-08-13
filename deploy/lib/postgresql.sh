#!/usr/bin/env bash
# PostgreSQL configuration convergence and bootstrap. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

PG_CONF_DIR=/etc/postgresql/17/main
FIREDASH_PGCONF=$PG_CONF_DIR/conf.d/90-firedash.conf
HBA=$PG_CONF_DIR/pg_hba.conf

firedash_pgconf_content() {
    cat <<'EOF'
listen_addresses = '127.0.0.1'
password_encryption = 'scram-sha-256'
EOF
}

firedash_hba_block() {
    cat <<'EOF'
# FireDash managed block BEGIN
host fire_backend database_owner      127.0.0.1/32 scram-sha-256
host fire_backend application_runtime 127.0.0.1/32 scram-sha-256
host fire_backend backup_role         127.0.0.1/32 scram-sha-256
# FireDash managed block END
EOF
}

# Converge the FireDash conf.d snippet. Echoes restart|reload|nochange.
postgres_config_converge() {
    install -d -m 0755 -o root -g root "$PG_CONF_DIR/conf.d"
    local old new changed=0 changed_listen=0 old_la new_la
    old=$(cat "$FIREDASH_PGCONF" 2>/dev/null || true)
    new=$(firedash_pgconf_content)
    if [[ -z $old ]]; then
        changed=1
        changed_listen=1
    else
        old_la=$(printf '%s\n' "$old" | sed -n "s/^[[:space:]]*listen_addresses[[:space:]]*=//p" | tr -d '[:space:]')
        new_la=$(printf '%s\n' "$new" | sed -n "s/^[[:space:]]*listen_addresses[[:space:]]*=//p" | tr -d '[:space:]')
        [[ $old_la == "$new_la" ]] || changed_listen=1
        [[ $old == "$new" ]] || changed=1
    fi
    if [[ $changed == 1 ]]; then
        write_string_atomic "$new" "$FIREDASH_PGCONF" 0644 root:root
    fi
    if [[ $changed_listen == 1 ]]; then
        echo restart
    elif [[ $changed == 1 ]]; then
        echo reload
    else
        echo nochange
    fi
}

# Converge the FireDash HBA block. Echoes changed|nochange.
postgres_hba_converge() {
    local block tmp changed=0
    block=$(firedash_hba_block)
    tmp=$(mktemp)
    if grep -q '# FireDash managed block BEGIN' "$HBA"; then
        awk -v block="$block" '
          BEGIN { inblock=0 }
          /# FireDash managed block BEGIN/ { inblock=1; print block; next }
          /# FireDash managed block END/ { inblock=0; next }
          !inblock { print }
        ' "$HBA" > "$tmp"
    else
        awk -v block="$block" '
          !inserted && $0 !~ /^[[:space:]]*(#|$)/ && $0 ~ /^(local|host|hostssl|hostnossl|hostgssenc|hostnogssenc)[[:space:]]/ {
            print block; inserted=1
          }
          { print }
          END { if (!inserted) { print block } }
        ' "$HBA" > "$tmp"
    fi
    if ! cmp -s "$tmp" "$HBA"; then
        chown --reference="$HBA" "$tmp" 2>/dev/null || chown postgres:postgres "$tmp"
        chmod --reference="$HBA" "$tmp" 2>/dev/null || chmod 0640 "$tmp"
        [[ -e "$HBA.pre-firedash" ]] || cp -a "$HBA" "$HBA.pre-firedash"
        mv -f "$tmp" "$HBA"
        changed=1
    else
        rm -f "$tmp"
    fi
    [[ $changed == 1 ]] && echo changed || echo nochange
}

postgres_apply_reload() {
    case "$1" in
        restart) log "restarting PostgreSQL (listen_addresses changed)"; pg_ctlcluster 17 main restart ;;
        reload) log "reloading PostgreSQL"; pg_ctlcluster 17 main reload ;;
        nochange) ;;
        *) die "unknown postgres reload action: $1" ;;
    esac
}

# Converge configuration and apply the correct reload/restart/no-op.
postgres_converge() {
    local conf_action hba_result
    conf_action=$(postgres_config_converge)
    hba_result=$(postgres_hba_converge)
    if [[ $conf_action == restart ]]; then
        postgres_apply_reload restart
    elif [[ $conf_action == reload || $hba_result == changed ]]; then
        postgres_apply_reload reload
    fi
}

# Run the production bootstrap SQL with secrets scoped to a subshell.
# The SQL file is opened by the root installer shell and fed to psql on stdin,
# because psql runs as the postgres OS user and cannot traverse the root-only
# stage-0 checkout under /tmp. bootstrap-production.sql has no \i/\ir includes,
# so stdin carries the complete script. runuser inherits the caller environment,
# so the FIREDASH_* variables reach the SQL's \getenv calls.
postgres_bootstrap() {
    local sql=${1:?}
    (
        export FIREDASH_DATABASE_OWNER_PASSWORD FIREDASH_APPLICATION_RUNTIME_PASSWORD FIREDASH_BACKUP_ROLE_PASSWORD
        FIREDASH_DATABASE_OWNER_PASSWORD=$(read_secret "$SECRET_DIR/database-owner-password")
        FIREDASH_APPLICATION_RUNTIME_PASSWORD=$(env_value "$ENV_FILE" POSTGRES_PASSWORD)
        FIREDASH_BACKUP_ROLE_PASSWORD=$(read_secret "$SECRET_DIR/backup-role-password")
        as_postgres psql -v ON_ERROR_STOP=1 -d postgres < "$sql"
    )
}
