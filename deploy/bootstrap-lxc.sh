#!/usr/bin/env bash
# Host bootstrap for a Debian 13 FireDash LXC node. Idempotent.
# Installs packages, service identities, systemd units, the PDF sanitizer boundary,
# and converges PostgreSQL configuration. It does NOT bootstrap roles, run migrations,
# deploy code, or create the administrator.
set -Eeuo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)

# shellcheck source=lib/common.sh
source "$SELF_DIR/lib/common.sh"
# shellcheck source=lib/postgresql.sh
source "$SELF_DIR/lib/postgresql.sh"
# shellcheck source=lib/systemd.sh
source "$SELF_DIR/lib/systemd.sh"

FIREDASH_REPO_ROOT=${FIREDASH_REPO_ROOT:-$ROOT}
export FIREDASH_REPO_ROOT

PACKAGES=(ca-certificates git sudo python3 python3-venv python3-dev build-essential
          libpq-dev libffi-dev libssl-dev libmagic1
          postgresql-17 postgresql-17-postgis-3 postgresql-17-postgis-3-scripts postgresql-client-17
          gdal-bin libgdal-dev libgeos-dev libproj-dev proj-bin
          qpdf nginx restic openssl curl)

require_root
is_debian_13 || die "Debian 13 (trixie) is required"
is_amd64 || die "amd64 architecture is required"
is_systemd || die "systemd must be PID 1"
[[ -d /sys/fs/cgroup ]] || die "cgroup filesystem is unavailable"
[[ $(df -Pm / | awk 'NR==2 {print $4}') -ge 2048 ]] || log_warn "less than 2 GiB free on root filesystem"
[[ $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) -ge 1024 ]] || log_warn "less than 1 GiB RAM available"
grep -qE 'container=(lxc|container)' /proc/1/environ 2>/dev/null || log_warn "LXC environment was not detected"

log "installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PACKAGES[@]}"

log "verifying qpdf is present"
command -v qpdf >/dev/null 2>&1 || die "qpdf is not installed"
log "qpdf version: $(qpdf --version 2>&1 | head -n1)"

systemctl enable --now postgresql

log "creating service identities and runtime directories"
"$ROOT/deploy/scripts/create-service-users.sh"

install -d -o root -g root -m 0755 /srv/firedash /srv/firedash/releases
install -d -o root -g root -m 0700 /var/lib/fire-backend/backup-staging /var/lib/fire-backend/restore-tests

log "installing tmpfiles, systemd units, PDF sanitizer boundary"
install -m 0644 "$ROOT/deploy/systemd/fire-backend.tmpfiles.conf" /etc/tmpfiles.d/fire-backend.conf
install_systemd_units

install -d -o root -g root -m 0755 /usr/local/lib/fire-backend
install -m 0755 -o root -g root "$ROOT/deploy/scripts/fire-pdf-sanitize" /usr/local/lib/fire-backend/fire-pdf-sanitize
install -m 0440 -o root -g root "$ROOT/deploy/sudoers/fire-pdf-sanitizer" /etc/sudoers.d/fire-pdf-sanitizer
visudo -cf /etc/sudoers.d/fire-pdf-sanitizer

systemd-tmpfiles --create

log "converging PostgreSQL configuration"
postgres_converge

log "host bootstrap complete."
