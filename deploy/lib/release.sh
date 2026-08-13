#!/usr/bin/env bash
# Immutable release construction and mutable activation. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

release_path() { printf '%s/%s' "$RELEASES_DIR" "$1"; }

# Build an immutable, SHA-addressed release with a completion marker.
build_release() {
    local sha=${FIREDASH_RESOLVED_SHA:?} root=${FIREDASH_REPO_ROOT:?} final staging actual
    final=$(release_path "$sha")

    if [[ -e "$final/.firedash-release-complete" ]]; then
        log "release $sha already built; reusing"
        return 0
    fi
    if [[ -e $final ]]; then
        die "release directory $final exists without a completion marker; refusing to overwrite (operator inspection required)"
    fi

    rm -rf "$RELEASES_DIR/.$sha.staging."*
    staging="$RELEASES_DIR/.$sha.staging.$$"
    install -d -m 0755 -o root -g root "$staging"

    log "extracting source for release $sha"
    git -C "$root" archive HEAD | tar -x -C "$staging"
    actual=$(git -C "$root" rev-parse HEAD)
    [[ $actual == "$sha" ]] || { rm -rf "$staging"; die "source SHA $actual does not match requested $sha"; }

    log "creating virtualenv"
    python3 -m venv "$staging/venv"

    log "installing Python requirements"
    "$staging/venv/bin/pip" install --no-cache-dir -r "$staging/requirements/base.txt"

    log "compile and import checks"
    "$staging/venv/bin/python" -m compileall -q "$staging/apps" "$staging/config" "$staging/manage.py"
    "$staging/venv/bin/python" -c 'import django; print("django", django.get_version())' >/dev/null

    : > "$staging/.firedash-release-complete"

    log "hardening release tree permissions (root-owned, group/world non-writable)"
    chown -R root:root "$staging"
    chmod -R go-w "$staging"

    log "promoting staging to $final"
    mv "$staging" "$final"
}

# Migrate, reapply grants, collectstatic, run checks, and atomically switch current.
activate_release() {
    local release=${FIREDASH_RELEASE:?} root=${FIREDASH_REPO_ROOT:?}
    local sql="$root/deploy/postgresql/roles.sql"

    log "running migrations as database_owner"
    (
        export DJANGO_SETTINGS_MODULE=config.settings.production
        load_env_file "$ENV_FILE"
        export POSTGRES_USER=database_owner
        export POSTGRES_PASSWORD
        POSTGRES_PASSWORD=$(read_secret "$SECRET_DIR/database-owner-password")
        "$release/venv/bin/python" "$release/manage.py" migrate --noinput
    )

    log "reapplying runtime/backup privileges"
    (
        export PGPASSWORD
        PGPASSWORD=$(read_secret "$SECRET_DIR/database-owner-password")
        psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U database_owner -d fire_backend -f "$sql"
    )

    log "collecting static files"
    (
        export DJANGO_SETTINGS_MODULE=config.settings.production
        load_env_file "$ENV_FILE"
        "$release/venv/bin/python" "$release/manage.py" collectstatic --noinput
    )

    log "Django deployment checks"
    (
        export DJANGO_SETTINGS_MODULE=config.settings.production
        load_env_file "$ENV_FILE"
        "$release/venv/bin/python" "$release/manage.py" check --deploy
        "$release/venv/bin/python" "$release/manage.py" migrate --check
    )

    log "switching current release to $release"
    ln -sfn "$release" "$CURRENT_LINK.new"
    mv -Tf "$CURRENT_LINK.new" "$CURRENT_LINK"
}
