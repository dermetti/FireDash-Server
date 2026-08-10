#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
REPOSITORY_URL=""
PACKAGES=(ca-certificates git openssh-client sudo python3 python3-venv python3-dev build-essential libpq-dev libffi-dev libssl-dev libmagic1 postgresql-17 postgresql-17-postgis-3 postgresql-17-postgis-3-scripts postgresql-client-17 gdal-bin libgdal-dev libgeos-dev libproj-dev proj-bin qpdf nginx restic)

die() { printf 'bootstrap-lxc: %s\n' "$*" >&2; exit 1; }
warn() { printf 'bootstrap-lxc warning: %s\n' "$*" >&2; }

while (($#)); do
    case "$1" in
        --repository-url) (($# >= 2)) || die "--repository-url requires an SSH URL"; REPOSITORY_URL=$2; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root"
. /etc/os-release
[[ ${ID:-} == debian && ${VERSION_ID:-} == 13 ]] || die "Debian 13 is required"
[[ $(dpkg --print-architecture) == amd64 ]] || die "amd64 is required"
[[ $(ps -p 1 -o comm=) == systemd ]] || die "systemd must be PID 1"
[[ -d /sys/fs/cgroup ]] || die "cgroup filesystem is unavailable"
[[ $(df -Pm / | awk 'NR==2 {print $4}') -ge 2048 ]] || warn "less than 2 GiB free on root filesystem"
[[ $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) -ge 1024 ]] || warn "less than 1 GiB RAM available"
grep -qE 'container=(lxc|container)' /proc/1/environ 2>/dev/null || warn "LXC environment was not detected"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"
systemctl enable --now postgresql

"$ROOT/deploy/scripts/create-service-users.sh"
getent group fire_deploy >/dev/null || groupadd --system fire_deploy
id fire_deploy >/dev/null 2>&1 || useradd --system --gid fire_deploy --home-dir /var/lib/fire-deploy --shell /usr/sbin/nologin --create-home fire_deploy
install -d -o fire_deploy -g fire_deploy -m 0700 /var/lib/fire-deploy/.ssh
install -d -o root -g root -m 0755 /srv/firedash /srv/firedash/releases
install -d -o root -g root -m 0700 /var/lib/fire-backend/backup-staging /var/lib/fire-backend/restore-tests

install -m 0644 "$ROOT/deploy/systemd/fire-backend.tmpfiles.conf" /etc/tmpfiles.d/fire-backend.conf
for unit in "$ROOT"/deploy/systemd/*.service "$ROOT"/deploy/systemd/*.socket "$ROOT"/deploy/systemd/*.timer; do
    [[ $(basename "$unit") == fire-pdf-sanitizer.service ]] && continue
    install -m 0644 "$unit" /etc/systemd/system/"$(basename "$unit")"
done
install -d -o root -g root -m 0755 /usr/local/lib/fire-backend
install -m 0755 -o root -g root "$ROOT/deploy/scripts/fire-pdf-sanitize" /usr/local/lib/fire-backend/fire-pdf-sanitize
install -m 0440 -o root -g root "$ROOT/deploy/sudoers/fire-pdf-sanitizer" /etc/sudoers.d/fire-pdf-sanitizer
visudo -cf /etc/sudoers.d/fire-pdf-sanitizer
systemctl daemon-reload
systemd-tmpfiles --create

PGCONF=/etc/postgresql/17/main/conf.d/90-firedash.conf
install -d -o root -g root -m 0755 "$(dirname "$PGCONF")"
[[ -e $PGCONF ]] || printf "listen_addresses = '127.0.0.1,::1'\npassword_encryption = 'scram-sha-256'\n" > "$PGCONF"
HBA=/etc/postgresql/17/main/pg_hba.conf
cp -an "$HBA" "$HBA.pre-firedash" || true
grep -q '# FireDash managed block' "$HBA" || cat >> "$HBA" <<'EOF'
# FireDash managed block
host fire_backend database_owner 127.0.0.1/32 scram-sha-256
host fire_backend application_runtime 127.0.0.1/32 scram-sha-256
host fire_backend backup_role 127.0.0.1/32 scram-sha-256
host fire_backend database_owner ::1/128 scram-sha-256
host fire_backend application_runtime ::1/128 scram-sha-256
host fire_backend backup_role ::1/128 scram-sha-256
EOF
systemctl reload postgresql

SECRETS=/etc/fire-backend/bootstrap-postgresql.env
if [[ -f $SECRETS ]]; then
    [[ $(stat -c %a "$SECRETS") == 600 ]] || die "$SECRETS must be mode 0600"
    set -a; . "$SECRETS"; set +a
    sudo -u postgres env \
        FIREDASH_DATABASE_OWNER_PASSWORD="$FIREDASH_DATABASE_OWNER_PASSWORD" \
        FIREDASH_APPLICATION_RUNTIME_PASSWORD="$FIREDASH_APPLICATION_RUNTIME_PASSWORD" \
        FIREDASH_BACKUP_ROLE_PASSWORD="$FIREDASH_BACKUP_ROLE_PASSWORD" \
        psql -v ON_ERROR_STOP=1 -d postgres -f "$ROOT/deploy/postgresql/bootstrap-production.sql"
else
    warn "PostgreSQL role bootstrap pending: install $SECRETS as root:root 0600"
fi

KEY=/var/lib/fire-deploy/.ssh/id_ed25519
if [[ ! -e $KEY ]]; then sudo -u fire_deploy ssh-keygen -q -t ed25519 -N '' -f "$KEY"; fi
printf 'FireDash deploy-key public key:\n'; cat "$KEY.pub"
if [[ -n $REPOSITORY_URL ]]; then
    [[ $REPOSITORY_URL =~ ^git@github\.com:[^/]+/[^/]+\.git$ ]] || die "repository URL must be git@github.com:owner/repository.git"
    sudo -u fire_deploy ssh-keyscan -H github.com >> /var/lib/fire-deploy/.ssh/known_hosts
    sudo -u fire_deploy git ls-remote "$REPOSITORY_URL" HEAD >/dev/null
    printf 'Read-only repository access verified.\n'
else
    printf 'Add the public key as a read-only GitHub deploy key, then rerun with --repository-url.\n'
fi
printf 'Bootstrap complete. Install host-local application configuration and credentials, then run deploy-release.sh.\n'
