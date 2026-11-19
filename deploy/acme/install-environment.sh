#!/usr/bin/env bash
# Explicit operator action, never called by install-local.sh or a renewal hook.
# Install only reviewed, hash-pinned wheels from a pre-fetched wheelhouse.
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} == 0 ]] || { echo "root required" >&2; exit 1; }
[[ $# == 1 && -d $1 ]] || { echo "usage: install-environment.sh WHEELHOUSE" >&2; exit 1; }
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 && $(dpkg --print-architecture) == amd64 ]] \
    || { echo "reviewed environment requires Debian 13 amd64" >&2; exit 1; }
exec 9>/run/lock/firedash-install.lock
flock -n 9 || { echo "FireDash installer/renewal active" >&2; exit 1; }
umask 077
here=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target=/opt/fire-backend-acme-5.7.0-multi-5.6.0
link=/opt/fire-backend-acme
[[ ! -e $target && ! -L $target && ! -e $link && ! -L $link ]] \
    || { echo "existing ACME environment retained; reviewed upgrade required" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info[:2] == (3, 13)'
install -d -m 0700 -o root -g root "$target"
python3 -m venv "$target"
"$target/bin/python" -m pip install --no-index --find-links "$1" \
    --require-hashes --only-binary=:all: -r "$here/requirements.lock"
"$target/bin/python" -m pip check
"$target/bin/certbot" --version
# Activation happens only after the complete wheel installation succeeds.
ln -s "$target" "$link"
echo "Reviewed isolated Certbot environment installed; application environment unchanged"
