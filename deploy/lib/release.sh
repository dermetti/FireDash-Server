#!/usr/bin/env bash
# Immutable release construction and mutable activation. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

release_path() { printf '%s/%s' "$RELEASES_DIR" "$1"; }

# Verify a release venv's console-script shebangs resolve at their final path.
verify_release_venv() {
    local release=${1:?} gunicorn="$1/venv/bin/gunicorn" shebang interp
    [[ -x $gunicorn ]] || die "release venv is missing an executable gunicorn: $gunicorn"
    shebang=$(head -n1 "$gunicorn" 2>/dev/null)
    [[ $shebang == '#!'* ]] || die "gunicorn has no shebang: $gunicorn"
    interp=${shebang#\#!}
    interp=${interp%% *}
    [[ -n $interp ]] || die "gunicorn shebang is empty: $gunicorn"
    [[ $interp == "$release/venv/bin/"* ]] || die "gunicorn shebang interpreter is outside the release venv: $interp"
    [[ -e $interp ]] || die "gunicorn shebang interpreter does not exist: $interp"
    log "verified release venv: gunicorn -> $interp"
}

# Build an immutable, SHA-addressed release with a completion marker.
# The virtualenv is created at its FINAL path (never relocated), so console-script
# shebangs (which embed an absolute interpreter path) always resolve.
build_release() {
    local sha=${FIREDASH_RESOLVED_SHA:?} root=${FIREDASH_REPO_ROOT:?} final staging actual
    final=$(release_path "$sha")

    if [[ -e "$final/.firedash-release-complete" ]]; then
        log "release $sha already built; reusing"
        return 0
    fi

    # Interrupted installer build: safely recoverable.
    if [[ -e "$final/.firedash-release-building" ]]; then
        log "removing interrupted installer build for release $sha"
        rm -rf "$final"
    fi

    # Unexpected final directory (not installer-owned, not complete): fail closed.
    if [[ -e $final ]]; then
        die "release directory $final exists without an installer completion marker; refusing to overwrite (operator inspection required)"
    fi

    # Remove stale installer-owned staging directories.
    rm -rf "$RELEASES_DIR/.$sha.staging."*

    # 1-2. Stage and validate the source in an installer-owned temp directory.
    staging="$RELEASES_DIR/.$sha.staging.$$"
    install -d -m 0755 -o root -g root "$staging"
    log "extracting source for release $sha"
    git -C "$root" archive HEAD | tar -x -C "$staging"
    actual=$(git -C "$root" rev-parse HEAD)
    [[ $actual == "$sha" ]] || { rm -rf "$staging"; die "source SHA $actual does not match requested $sha"; }

    # 3-4. Create the final directory in an incomplete state, then move source in.
    log "creating release directory $final"
    install -d -m 0755 -o root -g root "$final"
    : > "$final/.firedash-release-building"
    find "$staging" -mindepth 1 -maxdepth 1 -exec mv -f -t "$final" -- {} +
    rm -rf "$staging"

    # 5. Create the virtualenv directly at its final path (never relocate it).
    log "creating virtualenv at final path"
    python3 -m venv "$final/venv"

    # 6. Install requirements into the final-path venv.
    log "installing Python requirements"
    "$final/venv/bin/pip" install --no-cache-dir -r "$final/requirements/base.txt"

    # 7. Compile and import checks.
    log "compile and import checks"
    "$final/venv/bin/python" -m compileall -q "$final/apps" "$final/config" "$final/manage.py"
    "$final/venv/bin/python" -c 'import django; print("django", django.get_version())' >/dev/null

    # 8. Harden ownership and write permissions.
    log "hardening release tree permissions (root-owned, group/world non-writable)"
    chown -R root:root "$final"
    chmod -R go-w "$final"

    # 9. Verify the venv console-script shebangs resolve at the final path.
    verify_release_venv "$final"

    # 10. Mark complete last; remove the building marker.
    rm -f "$final/.firedash-release-building"
    : > "$final/.firedash-release-complete"
    log "release $sha built"
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
