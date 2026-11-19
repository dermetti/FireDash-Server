#!/usr/bin/env bash
# TLS validation and Nginx convergence. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

NGINX_SITE=/etc/nginx/sites-available/fire-backend
NGINX_ENABLED=/etc/nginx/sites-enabled/fire-backend

# Validate a TLS certificate/private-key pair for a hostname (RSA and EC generic).
validate_tls() {
    local cert=${1:-} key=${2:-} host=${3:-} cert_pub key_pub start start_ts now_ts
    [[ -f $cert ]] || die "FIREDASH_TLS_CERT_PATH: certificate not found"
    [[ -f $key ]] || die "FIREDASH_TLS_KEY_PATH: private key not found"
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 || die "FIREDASH_TLS_CERT_PATH: certificate does not parse"
    openssl pkey -in "$key" -noout >/dev/null 2>&1 || die "FIREDASH_TLS_KEY_PATH: private key does not parse"
    cert_pub=$(openssl x509 -in "$cert" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
    key_pub=$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
    [[ -n $cert_pub && $cert_pub == "$key_pub" ]] || die "TLS certificate and private key do not match"
    openssl x509 -in "$cert" -noout -checkhost "$host" 2>/dev/null | grep -q "does match certificate" || die "TLS certificate is not valid for FIREDASH_BASE_URL hostname"
    openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 || die "TLS certificate is expired"
    start=$(openssl x509 -in "$cert" -noout -startdate 2>/dev/null | cut -d= -f2-)
    start_ts=$(date -d "$start" +%s 2>/dev/null || true)
    now_ts=$(date +%s)
    [[ -n $start_ts && $start_ts -le $now_ts ]] || die "TLS certificate is not yet valid"
}

render_nginx_conf() {
    local host=$1 cert=$2 key=$3 template=$4 out=$5
    sed \
        -e "s|__SERVER_NAME__|$host|g" \
        -e "s|__TLS_CERT_PATH__|$cert|g" \
        -e "s|__TLS_KEY_PATH__|$key|g" \
        "$template" > "$out"
}

# The root-only lifecycle helper owns transactional site installation/rollback.
install_nginx() {
    python3 "${FIREDASH_REPO_ROOT:?}/deploy/lib/tls.py" nginx
}
