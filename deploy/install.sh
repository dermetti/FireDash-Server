#!/usr/bin/env bash
# Stage-0 FireDash installer bootstrap. Fully self-contained; fetches the public
# repository over HTTPS and delegates to deploy/install-local.sh from the checkout.
set -Eeuo pipefail

REPO=https://github.com/dermetti/FireDash-Server.git
REF=${FIREDASH_REF:-main}
PASSTHROUGH=()

die() { printf 'firedash: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --ref) (($# >= 2)) || die "--ref requires a value"; REF=$2; shift 2 ;;
        --ref=*) REF=${1#--ref=}; shift ;;
        *) PASSTHROUGH+=("$1"); shift ;;
    esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (use: curl -fsSL .../install.sh | sudo bash)"

# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
[[ ${ID:-} == debian && ${VERSION_ID:-} == 13 ]] || die "Debian 13 (trixie) is required"
[[ $(dpkg --print-architecture 2>/dev/null) == amd64 ]] || die "amd64 architecture is required"
[[ $(ps -p 1 -o comm= 2>/dev/null) == systemd ]] || die "systemd must be PID 1"

# Ensure minimal fetch prerequisites (curl/git commands, ca-certificates package).
need=""
command -v curl >/dev/null 2>&1 || need="$need curl"
command -v git >/dev/null 2>&1 || need="$need git"
dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null | grep -q 'install ok installed' || need="$need ca-certificates"
if [[ -n $need ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $need
fi

TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $REF =~ ^[0-9a-f]{40}$ ]]; then
    git init -q "$TMP"
    git -C "$TMP" remote add origin "$REPO"
    git -C "$TMP" fetch -q --depth 1 origin "$REF"
    git -C "$TMP" checkout -q FETCH_HEAD
else
    git clone -q --depth 1 --branch "$REF" "$REPO" "$TMP"
fi

SHA=$(git -C "$TMP" rev-parse HEAD)
if [[ $REF =~ ^[0-9a-f]{40}$ && $SHA != "$REF" ]]; then
    die "resolved SHA $SHA does not match requested $REF"
fi

export FIREDASH_RESOLVED_SHA=$SHA
export FIREDASH_REQUESTED_REF=$REF
export FIREDASH_REPO_ROOT=$TMP

printf 'firedash: requested ref: %s\n' "$REF"
printf 'firedash: resolved SHA: %s\n' "$SHA"

set +e
bash "$TMP/deploy/install-local.sh" "${PASSTHROUGH[@]}"
rc=$?
set -e
exit "$rc"
