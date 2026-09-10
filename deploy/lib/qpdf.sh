#!/usr/bin/env bash
# Pinned qpdf resolver for report-admission JSON encryption inspection.

QPDF_MIN_VERSION=12.4
QPDF_VENDOR_VERSION=12.4.1
QPDF_VENDOR_ROOT=${FIREDASH_QPDF_VENDOR_ROOT:-/opt/firedash/vendor/qpdf}
QPDF_VENDOR_BIN="$QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION/bin/qpdf"
QPDF_VENDOR_URL="https://github.com/qpdf/qpdf/releases/download/v${QPDF_VENDOR_VERSION}/qpdf-${QPDF_VENDOR_VERSION}-linux-x86_64.tar.gz"
QPDF_VENDOR_SHA256="db9122e88ec00c76ac6a14e09ffb92406db1773d47b968911ff6e69f28c09bf9"

qpdf_is_adequate() {
    local bin=$1 version schema
    [[ -x $bin ]] || return 1
    version=$($bin --version 2>/dev/null | awk 'NR==1 {print $NF}') || return 1
    dpkg --compare-versions "$version" ge "$QPDF_MIN_VERSION" || return 1
    # The structured JSON schema is the capability report-admission consumes;
    # merely matching a version string is intentionally insufficient.
    schema=$($bin --json-help 2>/dev/null) || return 1
    grep -q '"encrypt"' <<<"$schema" || return 1
    grep -q '"streammethod"' <<<"$schema" || return 1
    grep -q '"stringmethod"' <<<"$schema" || return 1
    grep -q '"filemethod"' <<<"$schema" || return 1
}

persist_qpdf_path() {
    local bin=$1 tmp
    install -d -m 0755 "$FIREDASH_ETC"
    write_string_atomic "$bin" "$FIREDASH_ETC/qpdf-path" 0644 root:root
    [[ -f $ENV_FILE ]] || return 0
    tmp=$(mktemp "$ENV_FILE.qpdf.XXXXXX")
    awk -F= '$1 != "OUTBOUND_MAIL_QPDF_BINARY" {print}' "$ENV_FILE" > "$tmp"
    printf 'OUTBOUND_MAIL_QPDF_BINARY=%s\n' "$bin" >> "$tmp"
    chmod 0640 "$tmp"; chown root:fire_backend "$tmp"
    mv -f "$tmp" "$ENV_FILE"
}

install_vendor_qpdf() {
    [[ $(dpkg --print-architecture) == amd64 ]] || die "no verified managed qpdf artifact for this architecture"
    if qpdf_is_adequate "$QPDF_VENDOR_BIN"; then printf '%s' "$QPDF_VENDOR_BIN"; return; fi
    local stage archive actual extracted
    stage=$(mktemp -d "$QPDF_VENDOR_ROOT/.qpdf-stage.XXXXXX")
    trap 'rm -rf "$stage"' RETURN
    archive="$stage/qpdf.tar.gz"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$QPDF_VENDOR_URL" -o "$archive" \
        || die "could not download pinned qpdf artifact"
    actual=$(sha256sum "$archive" | awk '{print $1}')
    [[ $actual == "$QPDF_VENDOR_SHA256" ]] || die "pinned qpdf checksum verification failed"
    tar -tzf "$archive" >/dev/null || die "pinned qpdf archive is malformed"
    tar -xzf "$archive" -C "$stage"
    extracted=$(find "$stage" -type f -path '*/bin/qpdf' -print -quit)
    [[ -n $extracted && $(find "$stage" -type f -path '*/bin/qpdf' | wc -l) -eq 1 ]] || die "unexpected qpdf archive layout"
    install -d -m 0755 "$(dirname "$QPDF_VENDOR_BIN")"
    install -m 0755 "$extracted" "$QPDF_VENDOR_BIN.new"
    mv -f "$QPDF_VENDOR_BIN.new" "$QPDF_VENDOR_BIN"
    qpdf_is_adequate "$QPDF_VENDOR_BIN" || die "managed qpdf lacks required JSON encryption capability"
    printf '%s' "$QPDF_VENDOR_BIN"
}

resolve_qpdf() {
    local candidate=${OUTBOUND_MAIL_QPDF_BINARY:-}
    if [[ -n $candidate ]]; then
        qpdf_is_adequate "$candidate" || die "operator qpdf path is inadequate"
        persist_qpdf_path "$candidate"; return
    fi
    if command -v qpdf >/dev/null 2>&1 && qpdf_is_adequate "$(command -v qpdf)"; then
        persist_qpdf_path "$(command -v qpdf)"; return
    fi
    candidate=$(install_vendor_qpdf)
    persist_qpdf_path "$candidate"
}
