#!/usr/bin/env bash
# Pinned qpdf resolver for report-admission JSON encryption inspection.

QPDF_MIN_VERSION=12.4
QPDF_VENDOR_VERSION=12.4.1
QPDF_VENDOR_ROOT=${FIREDASH_QPDF_VENDOR_ROOT:-/opt/firedash/vendor/qpdf}
QPDF_VENDOR_BIN="$QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION/bin/qpdf"
QPDF_VENDOR_ARCHIVE="qpdf-${QPDF_VENDOR_VERSION}-bin-linux-x86_64.zip"
QPDF_VENDOR_ARCHIVE_ROOT="${QPDF_VENDOR_ARCHIVE%.zip}"
QPDF_VENDOR_URL="https://github.com/qpdf/qpdf/releases/download/v${QPDF_VENDOR_VERSION}/${QPDF_VENDOR_ARCHIVE}"
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
    # Do not change an existing installation's permissions. A pre-existing
    # vendor root must already be root-owned and not writable by group/other.
    if [[ ! -e $QPDF_VENDOR_ROOT ]]; then
        install -d -o root -g root -m 0755 "$QPDF_VENDOR_ROOT" \
            || die "could not create qpdf vendor directory"
    elif [[ ! -d $QPDF_VENDOR_ROOT || -L $QPDF_VENDOR_ROOT ]]; then
        die "qpdf vendor path is not a directory"
    elif [[ $(stat -c '%U' "$QPDF_VENDOR_ROOT") != root ]] \
        || (( 8#$(stat -c '%a' "$QPDF_VENDOR_ROOT") & 0022 )); then
        die "qpdf vendor directory must be root-owned and not group/world writable"
    fi

    local stage archive actual extracted target previous
    # Staging deliberately occurs only after the parent exists. Do not begin a
    # download if this operation cannot be made safely.
    stage=$(mktemp -d "$QPDF_VENDOR_ROOT/.qpdf-stage.XXXXXX") \
        || die "could not create qpdf installation staging directory"
    archive="$stage/$QPDF_VENDOR_ARCHIVE"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$QPDF_VENDOR_URL" -o "$archive" \
        || { rm -rf "$stage"; die "could not download pinned qpdf artifact"; }
    actual=$(sha256sum "$archive" | awk '{print $1}')
    [[ $actual == "$QPDF_VENDOR_SHA256" ]] \
        || { rm -rf "$stage"; die "pinned qpdf checksum verification failed"; }
    # The upstream Linux bundle is relocatable: keep its exact top-level
    # directory so bin/qpdf continues to find ../lib through its runpath.
    python3 - "$archive" "$stage" "$QPDF_VENDOR_ARCHIVE_ROOT" <<'PY' \
        || { rm -rf "$stage"; die "pinned qpdf archive has an unexpected layout"; }
import pathlib
import sys
import zipfile

archive, destination, root = map(pathlib.Path, sys.argv[1:])
prefix = f"{root.as_posix()}/"
with zipfile.ZipFile(archive) as bundle:
    names = bundle.namelist()
    if not names or any(name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts for name in names):
        raise SystemExit(1)
    if any(not name.startswith(prefix) for name in names):
        raise SystemExit(1)
    required = {f"{prefix}bin/qpdf"}
    if not required.issubset(names) or not any(name.startswith(f"{prefix}lib/") for name in names):
        raise SystemExit(1)
    bundle.extractall(destination)
PY
    extracted="$stage/$QPDF_VENDOR_ARCHIVE_ROOT"
    [[ -f $extracted/bin/qpdf && -d $extracted/lib ]] \
        || { rm -rf "$stage"; die "pinned qpdf archive has an unexpected layout"; }
    chmod 0755 "$extracted/bin/qpdf"
    qpdf_is_adequate "$extracted/bin/qpdf" \
        || { rm -rf "$stage"; die "managed qpdf lacks required JSON encryption capability"; }

    target="$QPDF_VENDOR_ROOT/$QPDF_VENDOR_VERSION"
    if [[ -e $target || -L $target ]]; then
        previous="$QPDF_VENDOR_ROOT/.qpdf-invalid-${QPDF_VENDOR_VERSION}.$$.${RANDOM}"
        mv "$target" "$previous" \
            || { rm -rf "$stage"; die "could not preserve invalid qpdf installation"; }
    fi
    if ! mv "$extracted" "$target"; then
        [[ -n ${previous:-} ]] && mv "$previous" "$target" || true
        rm -rf "$stage"
        die "could not promote validated qpdf installation"
    fi
    rm -rf "$stage"
    [[ -z ${previous:-} ]] || rm -rf "$previous"
    qpdf_is_adequate "$QPDF_VENDOR_BIN" || die "managed qpdf installation verification failed"
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
