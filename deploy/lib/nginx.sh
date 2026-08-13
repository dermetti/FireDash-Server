#!/usr/bin/env bash
# TLS validation and Nginx convergence. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

NGINX_SITE=/etc/nginx/sites-available/fire-backend
NGINX_ENABLED=/etc/nginx/sites-enabled/fire-backend

# Validate a TLS certificate/private-key pair for a hostname (RSA and EC generic).
validate_tls() {
    local cert=${1:-} key=${2:-} host=${3:-} cert_pub key_pub
    [[ -f $cert ]] || die "TLS certificate not found: $cert"
    [[ -f $key ]] || die "TLS private key not found: $key"
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 || die "TLS certificate does not parse: $cert"
    openssl pkey -in "$key" -noout >/dev/null 2>&1 || die "TLS private key does not parse: $key"
    cert_pub=$(openssl x509 -in "$cert" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
    key_pub=$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
    [[ -n $cert_pub && $cert_pub == "$key_pub" ]] || die "TLS certificate and private key do not match"
    openssl x509 -in "$cert" -noout -checkhost "$host" >/dev/null 2>&1 || die "TLS certificate is not valid for hostname $host"
    openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 || die "TLS certificate is expired or not yet valid"
}

render_nginx_conf() {
    local host=$1 cert=$2 key=$3 template=$4 out=$5
    sed \
        -e "s|__SERVER_NAME__|$host|g" \
        -e "s|__TLS_CERT_PATH__|$cert|g" \
        -e "s|__TLS_KEY_PATH__|$key|g" \
        "$template" > "$out"
}

# Render, validate, and install Nginx config; reload or first-start as appropriate.
install_nginx() {
    local host=${FIREDASH_HOST:?} cert=${FIREDASH_TLS_CERT_PATH:?} key=${FIREDASH_TLS_KEY_PATH:?}
    local template=${FIREDASH_REPO_ROOT:?}/deploy/nginx/fire-backend.conf
    validate_tls "$cert" "$key" "$host"

    local rendered backup=""
    rendered=$(mktemp)
    render_nginx_conf "$host" "$cert" "$key" "$template" "$rendered"

    install -d -m 0755 -o root -g root /etc/nginx/sites-available /etc/nginx/sites-enabled
    if [[ -f $NGINX_SITE ]]; then
        backup=$(mktemp)
        cp -a "$NGINX_SITE" "$backup"
    fi

    install -m 0644 -o root -g root "$rendered" "$NGINX_SITE"
    rm -f "$rendered"

    if [[ ! -e $NGINX_ENABLED && ! -L $NGINX_ENABLED ]]; then
        ln -s "$NGINX_SITE" "$NGINX_ENABLED"
    fi
    rm -f /etc/nginx/sites-enabled/default

    if ! nginx -t; then
        log_err "nginx configuration validation failed; restoring previous configuration"
        if [[ -n $backup ]]; then
            install -m 0644 -o root -g root "$backup" "$NGINX_SITE"
            nginx -t || log_err "restored configuration also failed nginx -t"
        else
            rm -f "$NGINX_SITE" "$NGINX_ENABLED"
        fi
        [[ -n $backup ]] && rm -f "$backup"
        die "Nginx configuration is invalid"
    fi
    [[ -n $backup ]] && rm -f "$backup"

    if systemctl is-active --quiet nginx; then
        log "reloading nginx"
        systemctl reload nginx
    else
        log "starting nginx"
        systemctl enable --now nginx
    fi
}
