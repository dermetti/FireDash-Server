#!/usr/bin/env bash
# Shared helpers for FireDash deployment scripts. Source this file; do not execute.

if [[ -n ${FIREDASH_COMMON_SOURCED:-} ]]; then
    return 0
fi
FIREDASH_COMMON_SOURCED=1

# Canonical on-host paths (kept in one place to avoid drift; overridable for tests).
FIREDASH_ETC=${FIREDASH_ETC:-/etc/fire-backend}
INSTALL_CONF=$FIREDASH_ETC/install.conf
ENV_FILE=$FIREDASH_ETC/fire-backend.env
SECRET_DIR=$FIREDASH_ETC/credentials
SECRETS_MARKER=$FIREDASH_ETC/secrets-initialized
RELEASES_DIR=${FIREDASH_RELEASES_DIR:-/srv/firedash/releases}
CURRENT_LINK=${FIREDASH_CURRENT_LINK:-/srv/firedash/current}

# Initial-admin setup-URL state (root-only, never logged; overridable for tests).
ADMIN_SETUP_URL_FILE=${ADMIN_SETUP_URL_FILE:-/root/firedash-initial-admin-setup-url}
ADMIN_CREATED_MARKER=${ADMIN_CREATED_MARKER:-/run/firedash-admin-created}

log() { printf 'firedash: %s\n' "$*"; }
log_warn() { printf 'firedash warning: %s\n' "$*" >&2; }
log_err() { printf 'firedash error: %s\n' "$*" >&2; }
die() { log_err "$*"; exit 1; }

# Prompt for a still-missing value via /dev/tty (stdin may be the piped installer).
prompt_for() {
    local var=$1 label=$2
    if [[ -z ${!var:-} ]]; then
        if [[ ! -e /dev/tty ]] || [[ ! -w /dev/tty ]]; then
            die "missing required value: $var (set the environment variable or run interactively)"
        fi
        printf '%s: ' "$label" > /dev/tty
        IFS= read -r "$var" < /dev/tty || true
    fi
}

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (use: curl -fsSL .../install.sh | sudo bash)"
}

is_debian_13() {
    # shellcheck disable=SC1091
    . /etc/os-release 2>/dev/null || return 1
    [[ ${ID:-} == debian && ${VERSION_ID:-} == 13 ]]
}

is_amd64() { [[ $(dpkg --print-architecture 2>/dev/null) == amd64 ]]; }

is_systemd() { [[ $(ps -p 1 -o comm= 2>/dev/null) == systemd ]]; }

# Read the whitespace-trimmed contents of a text file (ASCII secrets only).
read_secret() {
    tr -d '[:space:]' < "$1" 2>/dev/null
}

# Read a single KEY=VALUE entry from an environment-style file.
env_value() {
    local file=$1 key=$2
    awk -F= -v k="$key" '$1==k { sub(/^[^=]*=/, ""); print; exit }' "$file"
}

# Export every KEY=VALUE line from a file into the current shell.
load_env_file() {
    local file=$1 key value
    [[ -f $file ]] || return 1
    while IFS='=' read -r key value; do
        [[ -z ${key:-} || ${key:-} == \#* ]] && continue
        export "$key=$value"
    done < "$file"
}

# Atomically install a file with the given mode/owner.
install_file_atomic() {
    local src=$1 dst=$2 mode=${3:-0644} owner=${4:-root:root}
    local tmp
    tmp=$(mktemp "${dst}.tmp.XXXXXX")
    cat "$src" > "$tmp"
    chmod "$mode" "$tmp"
    chown "$owner" "$tmp"
    mv -f "$tmp" "$dst"
}

# Atomically write a string to a file with the given mode/owner.
write_string_atomic() {
    local content=$1 dst=$2 mode=${3:-0644} owner=${4:-root:root}
    local tmp
    tmp=$(mktemp "${dst}.tmp.XXXXXX")
    printf '%s\n' "$content" > "$tmp"
    chmod "$mode" "$tmp"
    chown "$owner" "$tmp"
    mv -f "$tmp" "$dst"
}

# Run a command as the postgres system user without relying on sudo.
as_postgres() {
    runuser -u postgres -- "$@"
}

# Validate and normalize a base URL (https only, FQDN, no port/path/query/fragment/userinfo).
normalize_base_url() {
    local raw=${1:-} url host
    [[ -n $raw ]] || die "FIREDASH_BASE_URL is required (e.g. https://firedash.example.com)"
    url=${raw//[[:space:]]/}
    [[ $url == https://* ]] || die "base URL must use https:// (got: $raw)"
    url=${url#https://}
    url=${url%/}
    case "$url" in
        *@*) die "base URL must not contain userinfo" ;;
        *\?*) die "base URL must not contain a query string" ;;
        *\#*) die "base URL must not contain a fragment" ;;
        */*) die "base URL must not contain a path" ;;
        *:*) die "base URL must not contain a port" ;;
    esac
    host=$url
    [[ -n $host ]] || die "base URL is missing a hostname"
    [[ $host == *.* ]] || die "base URL hostname must be a fully-qualified domain name: $host"
    case "$host" in
        *[!A-Za-z0-9.-]*) die "base URL hostname is invalid: $host" ;;
    esac
    printf 'https://%s' "$host"
}

hostname_from_url() {
    printf '%s' "${1#https://}"
}
