#!/usr/bin/env bash
# Standalone deployment verification. Exit non-zero if any check fails.
set -Euo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)

# shellcheck source=lib/common.sh
source "$SELF_DIR/lib/common.sh"

FAIL=0
fail() { log_err "FAIL: $*"; FAIL=$((FAIL + 1)); }
ok() { log "OK: $*"; }

require_root

# -------- derive context --------
HOST=$(hostname_from_url "$(env_value "$INSTALL_CONF" FIREDASH_BASE_URL)")
RELEASE=$(readlink -f "$CURRENT_LINK")
OWNER_PW=$(read_secret "$SECRET_DIR/database-owner-password")
BACKUP_PW=$(read_secret "$SECRET_DIR/backup-role-password")
RUNTIME_PW=$(env_value "$ENV_FILE" POSTGRES_PASSWORD)

# -------- helpers --------
pg_as() { # role password statement...
    local role=$1 pw=$2 stmt=$3
    PGPASSWORD="$pw" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$role" -d fire_backend -tAc "$stmt"
}

# Privileged cluster-level introspection via the postgres OS identity. Used only
# where non-superuser roles (e.g. database_owner) lack catalog visibility.
pg_as_postgres() { # statement...
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -tAc "$1"
}

expect_sqlstate() { # role pw sqlstate stmt
    local role=$1 pw=$2 sqlstate=$3 stmt=$4 out rc
    out=$(PGPASSWORD="$pw" psql -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -h 127.0.0.1 -U "$role" -d fire_backend -c "$stmt" 2>&1)
    rc=$?
    if [[ $rc -eq 0 ]]; then
        fail "$role unexpectedly executed: $stmt"
        return 1
    fi
    if ! grep -q "$sqlstate" <<<"$out"; then
        fail "$role denial did not yield SQLSTATE $sqlstate (got: $out)"
        return 1
    fi
    ok "$role denied ($sqlstate): $stmt"
    return 0
}

# -------- host --------
log "=== host ==="
is_debian_13 && ok "Debian 13" || fail "not Debian 13"
is_amd64 && ok "amd64" || fail "not amd64"
is_systemd && ok "systemd PID 1" || fail "systemd is not PID 1"
for b in psql nginx curl openssl qpdf restic git; do
    command -v "$b" >/dev/null 2>&1 && ok "binary $b" || fail "binary $b missing"
done
[[ $(psql --version 2>/dev/null | grep -oE '[0-9]+' | head -n1) == 17 ]] && ok "PostgreSQL 17" || fail "PostgreSQL is not version 17"

# -------- database --------
log "=== database ==="
for r in database_owner application_runtime backup_role; do
    if pg_as database_owner "$OWNER_PW" "SELECT 1 FROM pg_roles WHERE rolname='$r'" | grep -q 1; then
        ok "role $r exists"
    else
        fail "role $r missing"
    fi
done
runtime_attrs=$(pg_as database_owner "$OWNER_PW" "SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls) FROM pg_roles WHERE rolname='application_runtime'")
[[ $runtime_attrs == t ]] && ok "application_runtime attributes hardened" || fail "application_runtime has unsafe attributes"

pg_as database_owner "$OWNER_PW" "SELECT 1 FROM pg_extension WHERE extname='postgis'" | grep -q 1 && ok "postgis installed" || fail "postgis missing"
pg_as database_owner "$OWNER_PW" "SELECT 1 FROM pg_extension WHERE extname='btree_gist'" | grep -q 1 && ok "btree_gist installed" || fail "btree_gist missing"

# HBA verification needs cluster-level visibility (pg_hba_file_rules), which
# database_owner lacks by design. Use the postgres OS identity, and report an
# introspection failure distinctly from a bad-auth failure.
if ! hba_rows=$(pg_as_postgres "SELECT user_name[1] || '|' || database[1] || '|' || coalesce(address,'') || '|' || coalesce(netmask,'') || '|' || coalesce(auth_method,'') || '|' || coalesce(error,'') || '|' || line_number FROM pg_hba_file_rules WHERE user_name && ARRAY['database_owner','application_runtime','backup_role'] ORDER BY line_number"); then
    fail "unable to inspect pg_hba_file_rules"
else
    for r in database_owner application_runtime backup_role; do
        row=$(printf '%s\n' "$hba_rows" | grep "^$r|" || true)
        if [[ -z $row ]]; then
            fail "HBA $r: FireDash rule missing"
            continue
        fi
        IFS='|' read -r _u db addr mask auth err ln <<<"$row"
        if [[ $db == fire_backend && $addr == 127.0.0.1 && $mask == 255.255.255.255 && $auth == scram-sha-256 && -z $err ]]; then
            ok "HBA $r: fire_backend 127.0.0.1/32 scram-sha-256"
        else
            fail "HBA $r: unexpected rule (db=$db addr=$addr mask=$mask auth=$auth err=$err)"
        fi
    done

    fire_max=$(printf '%s\n' "$hba_rows" | awk -F'|' '{print $NF}' | sort -n | tail -n1)
    broad_min=$(pg_as_postgres "SELECT min(line_number) FROM pg_hba_file_rules WHERE type='host' AND user_name @> ARRAY['all'] AND database @> ARRAY['all'] AND address='127.0.0.1'")
    if [[ -n $fire_max && ( -z $broad_min || $fire_max -lt $broad_min ) ]]; then
        ok "FireDash HBA rules precede broader host rules"
    else
        fail "FireDash HBA rules do not precede broader host rules"
    fi
fi

# login checks
pg_as application_runtime "$RUNTIME_PW" "SELECT 1" >/dev/null && ok "runtime login" || fail "runtime login failed"
pg_as database_owner "$OWNER_PW" "SELECT 1" >/dev/null && ok "database_owner login" || fail "database_owner login failed"
pg_as backup_role "$BACKUP_PW" "SELECT 1" >/dev/null && ok "backup_role login" || fail "backup_role login failed"

# -------- audit immutability --------
log "=== audit immutability ==="
trigger_func=$(pg_as database_owner "$OWNER_PW" "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname='audit_event_immutable' AND t.tgrelid='audit_event'::regclass")
trigger_enabled=$(pg_as database_owner "$OWNER_PW" "SELECT tgenabled FROM pg_trigger WHERE tgname='audit_event_immutable' AND tgrelid='audit_event'::regclass")
[[ $trigger_func == reject_audit_event_mutation ]] && ok "audit_event_immutable trigger attached to reject_audit_event_mutation" || fail "audit trigger function mismatch (got: $trigger_func)"
[[ $trigger_enabled == O || $trigger_enabled == A ]] && ok "audit_event_immutable trigger enabled" || fail "audit trigger missing/disabled (got: $trigger_enabled)"

expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "UPDATE audit_event SET id = id"
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "DELETE FROM audit_event"
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "TRUNCATE audit_event"

# The immutable trigger is statement-level, but only force a database_owner
# mutation probe when a valid audit row exists so the check is deterministic.
if pg_as database_owner "$OWNER_PW" "SELECT 1 FROM audit_event LIMIT 1" | grep -q 1; then
    expect_sqlstate database_owner "$OWNER_PW" P0001 "DELETE FROM audit_event"
else
    log_warn "audit_event has no rows; skipping database_owner trigger-fire probe"
fi

# -------- protected registry --------
log "=== protected registry ==="
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "UPDATE publications_datasettyperegistry SET code = code"
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "INSERT INTO publications_datasettyperegistry (code) VALUES ('__probe__')"
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "DELETE FROM publications_datasettyperegistry"
pg_as application_runtime "$RUNTIME_PW" "SELECT 1 FROM publications_datasettyperegistry LIMIT 1" >/dev/null && ok "runtime can read registry" || fail "runtime cannot read registry"

# -------- backup --------
log "=== backup ==="
dump=$(mktemp /var/lib/fire-backend/backup-staging/verify-dump.XXXXXX)
chmod 600 "$dump"
if PGPASSWORD="$BACKUP_PW" pg_dump --format=custom --file="$dump" --host=127.0.0.1 --username=backup_role fire_backend >/dev/null 2>&1; then
    ok "backup_role pg_dump (with data)"
else
    fail "backup_role pg_dump failed"
fi
if pg_restore --list "$dump" >/dev/null 2>&1; then
    ok "pg_restore --list"
else
    fail "pg_restore --list failed"
fi
rm -f "$dump"
expect_sqlstate backup_role "$BACKUP_PW" 42501 "UPDATE audit_event SET id = id"

# -------- application --------
log "=== application ==="
[[ -L $CURRENT_LINK ]] && ok "current symlink exists" || fail "current symlink missing"
[[ -n $RELEASE && $RELEASE == /srv/firedash/releases/* ]] && ok "current resolves to $RELEASE" || fail "current resolves unexpectedly"
[[ -x "$RELEASE/venv/bin/python" ]] && ok "release venv python" || fail "release venv python missing"
[[ -e "$RELEASE/.firedash-release-complete" ]] && ok "release completion marker" || fail "release completion marker missing"

# gunicorn console script: must be executable and its shebang interpreter must resolve.
gunicorn_bin="$RELEASE/venv/bin/gunicorn"
if [[ -x $gunicorn_bin ]]; then
    ok "gunicorn executable"
    gunicorn_shebang=$(head -n1 "$gunicorn_bin" 2>/dev/null)
    gunicorn_interp="${gunicorn_shebang#\#!}"
    gunicorn_interp="${gunicorn_interp%% *}"
    if [[ -n $gunicorn_interp && -e $gunicorn_interp ]]; then
        ok "gunicorn shebang interpreter resolves ($gunicorn_interp)"
    else
        fail "gunicorn shebang interpreter does not resolve ($gunicorn_interp)"
    fi
else
    fail "gunicorn missing or not executable"
fi

# exact deployed SHA
EXPECTED_SHA=${FIREDASH_RESOLVED_SHA:-}
[[ -n $EXPECTED_SHA ]] || EXPECTED_SHA=$(env_value "$INSTALL_CONF" FIREDASH_LAST_SUCCESSFUL_SHA)
if [[ -n $EXPECTED_SHA ]]; then
    if [[ $RELEASE == /srv/firedash/releases/$EXPECTED_SHA ]]; then
        ok "current points to expected SHA $EXPECTED_SHA"
    else
        fail "current points to $RELEASE, expected /srv/firedash/releases/$EXPECTED_SHA"
    fi
else
    log_warn "exact-SHA verification unavailable (no FIREDASH_RESOLVED_SHA or FIREDASH_LAST_SUCCESSFUL_SHA)"
fi

(
    export DJANGO_SETTINGS_MODULE=config.settings.production
    load_env_file "$ENV_FILE"
    if "$RELEASE/venv/bin/python" "$RELEASE/manage.py" check --deploy >/dev/null 2>&1; then
        ok "check --deploy"
    else
        fail "check --deploy failed"
    fi
    if "$RELEASE/venv/bin/python" "$RELEASE/manage.py" migrate --check >/dev/null 2>&1; then
        ok "migrate --check"
    else
        fail "migrate --check failed"
    fi
)

# -------- systemd --------
log "=== systemd ==="
for unit in fire-backend.socket fire-backend.service fire-publication-delivery.service fire-publication-build.service fire-publication-build.socket fire-publication-build.timer fire-publication-maintenance.service fire-publication-maintenance.timer fire-temporary-assignment-expiry.service fire-temporary-assignment-expiry.timer fire-stale-installation.service fire-stale-installation.timer fire-pdf-sanitizer@.service fire-pdf-sanitizer-broker.socket fire-pdf-sanitizer-broker@.service fire-backup.service fire-backup.timer fire-restore.service; do
    [[ -f /etc/systemd/system/$unit ]] && ok "unit $unit installed" || fail "unit $unit missing"
done
[[ $(systemctl is-enabled fire-backend.socket 2>/dev/null) == enabled ]] && ok "fire-backend.socket enabled" || fail "fire-backend.socket not enabled"
[[ $(systemctl is-enabled fire-pdf-sanitizer-broker.socket 2>/dev/null) == enabled ]] && ok "fire-pdf-sanitizer-broker.socket enabled" || fail "fire-pdf-sanitizer-broker.socket not enabled"
[[ $(systemctl is-active fire-pdf-sanitizer-broker.socket 2>/dev/null) == active ]] && ok "fire-pdf-sanitizer-broker.socket active" || fail "fire-pdf-sanitizer-broker.socket not active"
[[ $(systemctl is-enabled fire-backend.service 2>/dev/null) != enabled ]] && ok "fire-backend.service not directly enabled" || fail "fire-backend.service should not be enabled"
for t in fire-publication-build.timer fire-publication-maintenance.timer fire-temporary-assignment-expiry.timer fire-stale-installation.timer; do
    [[ $(systemctl is-enabled "$t" 2>/dev/null) == enabled ]] && ok "$t enabled" || fail "$t not enabled"
done
[[ $(systemctl is-enabled fire-publication-delivery.service 2>/dev/null) == enabled ]] && ok "fire-publication-delivery.service enabled" || fail "fire-publication-delivery.service not enabled"
[[ $(systemctl is-active fire-publication-delivery.service 2>/dev/null) == active ]] && ok "fire-publication-delivery.service active" || fail "fire-publication-delivery.service not active"
[[ $(systemctl is-enabled fire-publication-build.socket 2>/dev/null) == enabled ]] && ok "fire-publication-build.socket enabled" || fail "fire-publication-build.socket not enabled"
[[ $(systemctl is-active fire-publication-build.socket 2>/dev/null) == active ]] && ok "fire-publication-build.socket active" || fail "fire-publication-build.socket not active"
if systemctl cat fire-publication-build.socket 2>/dev/null | grep -q '^SocketGroup=fire_backend' \
    && systemctl cat fire-publication-build.socket 2>/dev/null | grep -q '^SocketMode=0660' \
    && systemctl cat fire-publication-build.socket 2>/dev/null | grep -q '^FileDescriptorName=publication-build-wake'; then
    ok "publication build wake socket is limited to fire_backend"
else
    fail "publication build wake socket permissions unexpected"
fi
if systemctl cat fire-publication-build.timer 2>/dev/null | grep -q '^OnCalendar=\*-\*-\* 00:05:00'; then
    ok "publication build timer is scheduled nightly at 00:05"
else
    fail "publication build timer schedule unexpected"
fi
if [[ $(stat -c '%U:%G:%a' /run/fire-backend/publication-build.sock 2>/dev/null) == "root:fire_backend:660" ]]; then
    ok "publication build wake socket owner/group/mode is constrained"
else
    fail "publication build wake socket owner/group/mode unexpected"
fi
if systemctl is-enabled fire-publication-worker.timer 2>/dev/null | grep -q enabled; then
    fail "obsolete fire-publication-worker.timer is enabled"
else
    ok "obsolete fire-publication-worker.timer retired"
fi
if systemctl is-active --quiet fire-publication-worker.service; then
    fail "obsolete fire-publication-worker.service is active"
else
    ok "obsolete fire-publication-worker.service is inactive"
fi

# credential separation
if systemctl cat fire-backend.service 2>/dev/null | grep -q 'publication-signing-public-key-ring'; then
    ok "web service loads public signing-key ring"
else
    fail "web service missing public signing-key ring credential"
fi
if systemctl cat fire-backend.service 2>/dev/null | grep -Eq 'publication-kek|publication-signing-key:'; then
    fail "web service loads private KEK/signing key"
else
    ok "web service does not load KEK/private signing key"
fi
if systemctl cat fire-publication-delivery.service 2>/dev/null | grep -q 'publication-kek' \
    && systemctl cat fire-publication-delivery.service 2>/dev/null | grep -q 'publication-signing-key' \
    && systemctl cat fire-publication-delivery.service 2>/dev/null | grep -q 'publication-signing-public-key-ring' \
    && systemctl cat fire-publication-delivery.service 2>/dev/null | grep -q -- '--delivery --forever --poll-seconds 2'; then
    ok "publication delivery worker has private credentials and delivery-only command"
else
    fail "publication delivery worker credentials or command unexpected"
fi
if systemctl cat fire-publication-build.service 2>/dev/null | grep -q -- 'process_publication_jobs --build'; then
    ok "publication build worker is build-only"
else
    fail "publication build worker command unexpected"
fi
if systemctl cat fire-publication-build.service 2>/dev/null | grep -q 'publication-kek' \
    && systemctl cat fire-publication-build.service 2>/dev/null | grep -q 'publication-signing-key' \
    && systemctl cat fire-publication-build.service 2>/dev/null | grep -q 'publication-signing-public-key-ring'; then
    ok "publication build worker has required private credentials"
else
    fail "publication build worker private credentials unexpected"
fi
if systemctl cat fire-publication-maintenance.service 2>/dev/null | grep -Eq 'publication-kek|publication-signing-key:'; then
    fail "maintenance service must not load KEK/private signing key"
else
    ok "maintenance service does not load KEK/private signing key"
fi
if runuser -u fire_backend -- sudo -n -l >/dev/null 2>&1; then
    fail "fire_backend has passwordless sudo privileges"
else
    ok "fire_backend has no passwordless sudo privilege"
fi

# backend privilege boundary: NoNewPrivileges must remain enforced.
if systemctl cat fire-backend.service 2>/dev/null | grep -Eq '^NoNewPrivileges=(true|yes)'; then
    ok "fire-backend.service NoNewPrivileges enforced"
else
    fail "fire-backend.service missing NoNewPrivileges=true"
fi

# sanitizer broker must carry no application secrets and no credential material.
if systemctl cat fire-pdf-sanitizer-broker@.service 2>/dev/null | grep -Eq 'publication-kek|publication-signing-key|LoadCredential|EnvironmentFile'; then
    fail "sanitizer broker must not load credentials/environment"
else
    ok "sanitizer broker loads no credentials/environment"
fi

# inetd-style stdio: accepted connection on stdin, journal on stderr, stdout inherited.
if systemctl cat fire-pdf-sanitizer-broker.socket 2>/dev/null | grep -q '^Accept=yes'; then
    ok "sanitizer broker socket Accept=yes"
else
    fail "sanitizer broker socket missing Accept=yes"
fi
if systemctl cat fire-pdf-sanitizer-broker@.service 2>/dev/null | grep -q '^StandardInput=socket'; then
    ok "sanitizer broker StandardInput=socket"
else
    fail "sanitizer broker missing StandardInput=socket"
fi
if systemctl cat fire-pdf-sanitizer-broker@.service 2>/dev/null | grep -q '^StandardOutput=inherit'; then
    ok "sanitizer broker StandardOutput=inherit"
else
    fail "sanitizer broker missing StandardOutput=inherit"
fi
if systemctl cat fire-pdf-sanitizer-broker@.service 2>/dev/null | grep -q '^StandardError=journal'; then
    ok "sanitizer broker StandardError=journal"
else
    fail "sanitizer broker missing StandardError=journal"
fi

# obsolete sudo handoff must be absent; the socket broker must be present.
[[ ! -e /etc/sudoers.d/fire-pdf-sanitizer ]] && ok "obsolete sudoers rule removed" || fail "obsolete sudoers rule still installed"
[[ ! -e /usr/local/lib/fire-backend/fire-pdf-sanitize ]] && ok "obsolete sudo wrapper removed" || fail "obsolete sudo wrapper still installed"
if [[ -x /usr/local/lib/fire-backend/fire-pdf-sanitizer-broker ]]; then
    ok "sanitizer broker executable present"
else
    fail "sanitizer broker executable missing"
fi
[[ $(stat -c '%U:%G:%a' /usr/local/lib/fire-backend/fire-pdf-sanitizer-broker) == "root:root:755" ]] && ok "sanitizer broker root:root 0755" || fail "sanitizer broker ownership/mode unexpected"
if [[ -S /run/fire-pdf-sanitizer-broker/broker.sock ]]; then
    [[ $(stat -c '%U:%G:%a' /run/fire-pdf-sanitizer-broker/broker.sock) == "root:fire_backend:660" ]] && ok "sanitizer broker socket root:fire_backend 0660" || fail "sanitizer broker socket ownership/mode unexpected"
else
    fail "sanitizer broker socket not present"
fi

# -------- filesystem / credentials --------
log "=== filesystem / credentials ==="
[[ $(stat -c '%a' "$SECRET_DIR") == 700 ]] && ok "credentials dir 0700" || fail "credentials dir mode $(stat -c '%a' "$SECRET_DIR")"
for f in database-owner-password backup-role-password publication-kek publication-signing-key publication-signing-public-key publication-signing-public-key-ring.json; do
    [[ $(stat -c '%U:%G:%a' "$SECRET_DIR/$f") == "root:root:600" ]] && ok "$f root:root 0600" || fail "$f has unexpected ownership/mode"
done
if "$RELEASE/venv/bin/python" - "$SECRET_DIR/publication-signing-key" \
    "$SECRET_DIR/publication-signing-public-key" \
    "$SECRET_DIR/publication-signing-public-key-ring.json" \
    "$(env_value "$ENV_FILE" PUBLICATION_SIGNING_KEY_VERSION)" <<'PY'
import base64
import json
import re
import sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_path, public_path, ring_path, active_version = (
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] or "1"
)
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", active_version):
    raise SystemExit(1)
document = json.loads(ring_path.read_text(encoding="ascii"))
keys = document.get("keys") if isinstance(document, dict) and set(document) == {"keys"} else None
if not isinstance(keys, dict):
    raise SystemExit(1)
key = keys.get(active_version)
if not isinstance(key, str):
    raise SystemExit(1)
public_key = public_path.read_bytes()
private_key = private_path.read_bytes()
if len(private_key) != 32 or Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw() != public_key:
    raise SystemExit(1)
if base64.b64decode(key.encode("ascii"), validate=True) != public_key:
    raise SystemExit(1)
PY
then
    ok "active publication private/public pair matches the retained public-key ring"
else
    fail "publication public-key ring is invalid or omits the active key"
fi
[[ $(stat -c '%U:%G:%a' "$ENV_FILE") == "root:fire_backend:640" ]] && ok "fire-backend.env root:fire_backend 0640" || fail "fire-backend.env ownership/mode unexpected"
[[ $(stat -c '%U:%G:%a' /var/lib/fire-backend/publications) == "fire_publication:fire_nginx:2750" ]] && ok "publications 2750" || fail "publications ownership/mode unexpected"
[[ $(stat -c '%U:%G:%a' /var/lib/fire-backend/publications/.tmp) == "fire_publication:fire_publication:700" ]] && ok "publications/.tmp 0700" || fail "publications/.tmp ownership/mode unexpected"
[[ $(stat -c '%U:%G:%a' /var/lib/fire-backend/fire-plans) == "fire_backend:fire_backend:750" ]] && ok "fire-plans 0750" || fail "fire-plans ownership/mode unexpected"
[[ $(stat -c '%U:%G:%a' /var/lib/fire-backend/import-staging) == "fire_backend:fire_backend:750" ]] && ok "import staging 0750" || fail "import staging ownership/mode unexpected"

neg_read() { # user path description
    local user=$1 path=$2 desc=$3
    if runuser -u "$user" -- test -r "$path" 2>/dev/null; then
        fail "$desc: $user can read $path"
    else
        ok "$desc: $user cannot read $path"
    fi
}

# Publication artifacts are served by Nginx workers. This deployment intentionally
# relies on the Debian default www-data worker user: the repo does not override the
# `user` directive, and create-service-users.sh grants www-data the fire_nginx group
# for final-artifact (0640/2750) access. Resolve the effective worker identity so the
# negative-access checks below target the identity that actually serves files.
nginx_user=www-data
if [[ -f /etc/nginx/nginx.conf ]]; then
    configured=$(awk '$1 == "user" {print $2; exit}' /etc/nginx/nginx.conf | tr -d ';')
    if [[ -n $configured ]]; then
        nginx_user=$configured
    fi
fi
if [[ $nginx_user != www-data ]]; then
    fail "Nginx worker user is '$nginx_user'; expected www-data"
else
    ok "Nginx worker user is www-data"
fi

# The protected artifact location must remain an internal X-Accel target and retain
# Django's cryptographic ETag instead of generating an mtime/size static-file ETag.
nginx_site=/etc/nginx/sites-available/fire-backend
if [[ ! -f $nginx_site ]]; then
    fail "Nginx FireDash site missing"
else
    protected_dataset_location=$(awk '
        $1 == "location" && $2 == "/internal-protected-datasets/" && $3 == "{" {
            in_location = 1
            next
        }
        in_location && $1 == "}" { exit }
        in_location { print }
    ' "$nginx_site")
    if [[ -z $protected_dataset_location ]]; then
        fail "Nginx protected dataset location missing"
    else
        grep -Eq '^[[:space:]]*internal;[[:space:]]*$' <<<"$protected_dataset_location" \
            && ok "Nginx protected dataset location is internal" \
            || fail "Nginx protected dataset location is not internal"
        grep -Eq '^[[:space:]]*alias[[:space:]]+/var/lib/fire-backend/publications/;[[:space:]]*$' \
            <<<"$protected_dataset_location" \
            && ok "Nginx protected dataset alias is canonical" \
            || fail "Nginx protected dataset alias is not canonical"
        grep -Eq '^[[:space:]]*etag[[:space:]]+off;[[:space:]]*$' <<<"$protected_dataset_location" \
            && ok "Nginx protected dataset static ETag disabled" \
            || fail "Nginx protected dataset static ETag must be disabled"
        grep -Eq '^[[:space:]]*add_header[[:space:]]+ETag[[:space:]]+\$upstream_http_etag[[:space:]]+always;[[:space:]]*$' \
            <<<"$protected_dataset_location" \
            && ok "Nginx protected dataset preserves upstream ETag" \
            || fail "Nginx protected dataset does not preserve upstream ETag"
    fi
fi

neg_read fire_backend "$SECRET_DIR/publication-kek" "publication KEK"
neg_read fire_backend "$SECRET_DIR/publication-signing-key" "private signing key"
neg_read www-data "$SECRET_DIR/publication-kek" "credential file"
neg_read www-data "$ENV_FILE" "runtime env"
neg_read www-data /var/lib/fire-backend/fire-plans "fire-plan source"
neg_read www-data /var/lib/fire-backend/publications/.tmp "publication temp (nginx worker)"
neg_read fire_pdf_sanitizer "$ENV_FILE" "runtime env"

# release tree must not be writable by service users
if runuser -u fire_backend -- test -w "$RELEASE" 2>/dev/null; then
    fail "fire_backend can write release tree"
else
    ok "fire_backend cannot write release tree"
fi
if runuser -u fire_publication -- test -w "$RELEASE" 2>/dev/null; then
    fail "fire_publication can write release tree"
else
    ok "fire_publication cannot write release tree"
fi

# env must not contain forbidden credential material
if grep -Eq '^(DATABASE_OWNER_PASSWORD|BACKUP_ROLE_PASSWORD|PUBLICATION_KEK|PUBLICATION_SIGNING_KEY)=' "$ENV_FILE"; then
    fail "fire-backend.env contains forbidden secret variable names"
else
    ok "fire-backend.env has no forbidden secret variable names"
fi

# -------- HTTP (local --resolve) --------
log "=== HTTP ==="
if curl -fsS --resolve "$HOST:443:127.0.0.1" "https://$HOST/health/live" >/dev/null 2>&1; then
    ok "HTTPS liveness (/health/live)"
else
    fail "HTTPS liveness failed"
fi
if curl -fsS --resolve "$HOST:443:127.0.0.1" "https://$HOST/health/ready" >/dev/null 2>&1; then
    ok "HTTPS readiness (/health/ready)"
else
    fail "HTTPS readiness failed"
fi

# -------- summary --------
echo
if [[ $FAIL -eq 0 ]]; then
    log "verification passed"
else
    log_err "verification FAILED with $FAIL error(s)"
    exit 1
fi
