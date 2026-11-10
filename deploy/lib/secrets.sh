#!/usr/bin/env bash
# Secret generation, validation, and commit. Source this file; do not execute.

_LIB_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$_LIB_DIR/common.sh"

# Derive the raw 32-byte Ed25519 public key from a raw 32-byte seed file.
derive_public_key() {
    local release=$1 seed=$2 out=$3
    "$release/venv/bin/python" - "$seed" > "$out" <<'PY'
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
seed = open(sys.argv[1], "rb").read()
assert len(seed) == 32
sys.stdout.buffer.write(Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw())
PY
}

# Return the operator-selected proxy, preserving a legacy runtime value while
# install.conf is being introduced as its canonical persistent source.
effective_outbound_mail_https_proxy() {
    if [[ ${FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY+x} ]]; then
        printf '%s' "$FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY"
    elif [[ -f $ENV_FILE ]]; then
        env_value "$ENV_FILE" OUTBOUND_MAIL_HTTPS_PROXY
    fi
}

validate_outbound_mail_https_proxy() {
    local release=$1 proxy_url=$2
    FIREDASH_PROXY_TO_VALIDATE="$proxy_url" PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}" "$release/venv/bin/python" - <<'PY'
import os
from apps.outbound_mail.http_transport import (
    OutboundMailTransportError,
    validate_outbound_mail_https_proxy,
)

try:
    validate_outbound_mail_https_proxy(os.environ["FIREDASH_PROXY_TO_VALIDATE"])
except (OutboundMailTransportError, TypeError, ValueError):
    raise SystemExit(1) from None
PY
}

# Render /etc/fire-backend/fire-backend.env. Empty runtime_password/secret_key reuse existing values.
render_env() {
    local tmp
    tmp=$(mktemp)
    chmod 0600 "$tmp"
    if ! FIREDASH_RUNTIME_PASSWORD=${1:-} FIREDASH_DJANGO_SECRET=${2:-} \
        FIREDASH_HOST=${3:-} python3 "$_LIB_DIR/configuration.py" render > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    install_file_atomic "$tmp" "$ENV_FILE" 0640 root:fire_backend
    rm -f "$tmp"
}

report_transfer_active_key() {
    local version=""
    [[ ! -f $ENV_FILE ]] || version=$(env_value "$ENV_FILE" REPORT_TRANSFER_ACTIVE_KEY)
    [[ -n $version ]] || version=1
    [[ $version =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "REPORT_TRANSFER_ACTIVE_KEY is invalid"
    printf '%s' "$version"
}

# Before migrations: absence of tables is normal on pre-feature upgrades, but
# connection/catalog failures are never evidence that generation is safe.
report_transfer_initial_enablement_safe() {
    local spool=${FIREDASH_REPORT_SPOOL:-/var/spool/firedash-reports} result
    [[ ! -e $FIREDASH_ETC/report-transfer-initialized ]] || return 1
    [[ ! -L $spool ]] || return 1
    if [[ -e $spool ]]; then
        [[ -d $spool ]] || return 1
        result=$(find "$spool" -mindepth 1 -maxdepth 1 -print -quit) || return 1
        [[ -z $result ]] || return 1
    fi
    result=$(as_postgres psql -X -v ON_ERROR_STOP=1 -d postgres -tAc \
        "SELECT count(*) FROM pg_database WHERE datname='fire_backend'") || return 1
    [[ $result != 0 ]] || return 0
    [[ $result == 1 ]] || return 1
    result=$(as_postgres psql -X -v ON_ERROR_STOP=1 -d fire_backend -tAc \
        "SELECT to_regclass('public.report_transfer_reportsubmission') IS NOT NULL") || return 1
    if [[ $result == t ]]; then
        result=$(as_postgres psql -X -v ON_ERROR_STOP=1 -d fire_backend -tAc \
            'SELECT EXISTS (SELECT 1 FROM public.report_transfer_reportsubmission)') || return 1
        [[ $result == f ]]
    elif [[ $result == f ]]; then
        # A previously migrated but now missing table is not a pre-feature DB.
        result=$(as_postgres psql -X -v ON_ERROR_STOP=1 -d fire_backend -tAc \
            "SELECT to_regclass('public.django_migrations') IS NOT NULL") || return 1
        [[ $result == t ]] || return 1
        result=$(as_postgres psql -X -v ON_ERROR_STOP=1 -d fire_backend -tAc \
            "SELECT EXISTS (SELECT 1 FROM public.django_migrations WHERE app='report_transfer')") || return 1
        [[ $result == f ]]
    else
        return 1
    fi
}

validate_report_transfer_keyring() {
    local release=$1 credential=$2 version=$3
    PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}" "$release/venv/bin/python" \
        "$release/deploy/scripts/report-transfer-credential.py" validate "$credential" "$version"
}

ensure_report_transfer_keyring() {
    local release=$1 credential="$SECRET_DIR/report-transfer-keyring.json" version staging
    [[ ! -L $SECRET_DIR && -d $SECRET_DIR ]] \
        && [[ $(stat -c '%U:%G:%a' "$SECRET_DIR") == root:root:700 ]] \
        || die "Report Transfer credentials directory must be a real root:root 0700 directory"
    version=$(report_transfer_active_key) || return 1
    if [[ -e $credential || -L $credential ]]; then
        [[ ! -L $credential && -f $credential ]] \
            && [[ $(stat -c '%U:%G:%a' "$credential") == root:root:600 ]] \
            && validate_report_transfer_keyring "$release" "$credential" "$version" \
            || die "invalid Report Transfer keyring or permissions at $credential; restore/repair without replacing any retained key versions"
    else
        report_transfer_initial_enablement_safe \
            || die "Report Transfer keyring missing and safe initial enablement cannot be proved; restore $credential from protected credential backup before retrying"
        staging=$(mktemp "$SECRET_DIR/.report-transfer.XXXXXX") || return 1
        chmod 0600 "$staging"
        if ! PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}" "$release/venv/bin/python" \
            "$release/deploy/scripts/report-transfer-credential.py" generate "$staging" "$version" \
            || ! validate_report_transfer_keyring "$release" "$staging" "$version"; then
            rm -f "$staging"
            die "could not generate a valid Report Transfer keyring"
        fi
        install_file_atomic "$staging" "$credential" 0600 root:root || { rm -f "$staging"; return 1; }
        rm -f "$staging"
        validate_report_transfer_keyring "$release" "$credential" "$version" \
            || die "installed Report Transfer keyring is invalid; restore credential"
    fi
    write_string_atomic '1' "$FIREDASH_ETC/report-transfer-initialized" 0600 root:root
}

# The following three helpers intentionally use the same Python implementation
# as Django's application-secret runtime.  Do not duplicate the key-ring schema
# in shell: deployment-generated KEKs must be accepted by the runtime verbatim.
application_secret_kek_version() {
    local version=1
    if [[ -f $ENV_FILE ]]; then
        version=$(env_value "$ENV_FILE" APPLICATION_SECRET_KEK_VERSION)
    fi
    [[ -n $version ]] || version=1
    printf '%s' "$version"
}

generate_application_secret_keyring() {
    local release=$1 output=$2 version=$3
    PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}" "$release/venv/bin/python" - "$version" > "$output" <<'PY'
import sys
from apps.application_secrets.crypto import generate_application_secret_keyring

sys.stdout.buffer.write(generate_application_secret_keyring(initial_version=sys.argv[1]))
PY
}

validate_application_secret_keyring() {
    local release=$1 credential=$2 active_version=$3
    PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}" "$release/venv/bin/python" - "$credential" "$active_version" <<'PY'
import sys
from pathlib import Path

from apps.application_secrets.crypto import (
    ApplicationSecretCipher,
    ApplicationSecretKeyringValidationError,
    parse_application_secret_keyring,
)

try:
    keys = parse_application_secret_keyring(Path(sys.argv[1]).read_bytes())
    ApplicationSecretCipher(keys=keys, active_version=sys.argv[2])
except OSError:
    raise SystemExit("missing") from None
except ApplicationSecretKeyringValidationError as error:
    raise SystemExit(error.code) from None
except ValueError:
    raise SystemExit("invalid_active_key_version") from None
PY
}

# Provision only an absent credential. Existing material is always validated,
# never regenerated: credentials already encrypted with it must remain usable.
ensure_application_secret_keyring() {
    local release=$1 credential="$SECRET_DIR/application-secret-kek-ring.json" version staging reason
    version=$(application_secret_kek_version)
    if [[ -e $credential || -L $credential ]]; then
        if ! reason=$(validate_application_secret_keyring "$release" "$credential" "$version" 2>&1); then
            die "application-secret KEK ring at $credential is invalid ($reason); restore or repair that credential without replacing its key material"
        fi
        return 0
    fi

    staging=$(mktemp)
    if ! generate_application_secret_keyring "$release" "$staging" "$version"; then
        rm -f "$staging"
        die "could not generate the initial application-secret KEK ring"
    fi
    if ! reason=$(validate_application_secret_keyring "$release" "$staging" "$version" 2>&1); then
        rm -f "$staging"
        die "generated application-secret KEK ring is invalid ($reason)"
    fi
    install_file_atomic "$staging" "$credential" 0600 root:root
    rm -f "$staging"
    if ! reason=$(validate_application_secret_keyring "$release" "$credential" "$version" 2>&1); then
        die "provisioned application-secret KEK ring at $credential is invalid ($reason)"
    fi
}

# Preserve every historical public key while ensuring the active private/public
# pair is represented by its configured immutable version.  The ring is loaded
# as a public-only systemd credential by web and publication services.
ensure_public_signing_key_ring() {
    local release=$1 mode=${2:-install} active_version tmp
    active_version=$(env_value "$ENV_FILE" PUBLICATION_SIGNING_KEY_VERSION)
    [[ -n $active_version ]] || active_version=1
    tmp=$(mktemp)
    chmod 0600 "$tmp"
    "$release/venv/bin/python" - "$SECRET_DIR/publication-signing-public-key" \
        "$SECRET_DIR/publication-signing-public-key-ring.json" "$active_version" > "$tmp" <<'PY' || { rm -f "$tmp"; return 1; }
import base64
import json
import re
import sys
from pathlib import Path

public_path = Path(sys.argv[1])
ring_path = Path(sys.argv[2])
active_version = sys.argv[3]
version_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
if not version_re.fullmatch(active_version):
    raise SystemExit("PUBLICATION_SIGNING_KEY_VERSION is invalid")

def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("public signing-key ring has duplicate keys")
        result[key] = value
    return result

public_key = public_path.read_bytes()
if len(public_key) != 32:
    raise SystemExit("active publication signing public key must be exactly 32 bytes")
if ring_path.exists():
    document = json.loads(ring_path.read_text(encoding="ascii"), object_pairs_hook=no_duplicate_keys)
    if not isinstance(document, dict) or set(document) != {"keys"} or not isinstance(document["keys"], dict):
        raise SystemExit("public signing-key ring is invalid")
    keys = document["keys"]
else:
    keys = {}
for version, encoded_key in keys.items():
    if not isinstance(version, str) or not version_re.fullmatch(version):
        raise SystemExit("public signing-key ring has an invalid version")
    if not isinstance(encoded_key, str):
        raise SystemExit("public signing-key ring has an invalid key")
    try:
        decoded = base64.b64decode(encoded_key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise SystemExit("public signing-key ring has an invalid key") from error
    if len(decoded) != 32:
        raise SystemExit("public signing-key ring key must be exactly 32 bytes")
active_encoded = base64.b64encode(public_key).decode("ascii")
if active_version in keys and keys[active_version] != active_encoded:
    raise SystemExit("active public key does not match its existing ring version")
if active_version in keys:
    # A valid existing keyring remains byte-for-byte unchanged.
    sys.stdout.write(ring_path.read_text(encoding="ascii"))
else:
    keys[active_version] = active_encoded
    sys.stdout.write(json.dumps({"keys": keys}, sort_keys=True, separators=(",", ":")) + "\n")
PY
    if [[ $mode == install ]]; then
        install_file_atomic "$tmp" "$SECRET_DIR/publication-signing-public-key-ring.json" 0600 root:root
    fi
    rm -f "$tmp"
}

# Generate and commit a full secret set. Only safe in PRISTINE / BOOTSTRAP_INCOMPLETE.
generate_and_commit_secrets() {
    local release=$1 host=$2 staging runtime_password secret_key application_secret_version f
    rm -rf /etc/fire-backend/.secrets-staging.*
    staging=$(mktemp -d /etc/fire-backend/.secrets-staging.XXXXXX)
    chmod 700 "$staging"

    openssl rand -hex 32 > "$staging/database-owner-password"
    openssl rand -hex 32 > "$staging/backup-role-password"
    runtime_password=$(openssl rand -hex 32)
    secret_key=$("$release/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(64))')

    "$release/venv/bin/python" -c 'import secrets, sys; sys.stdout.buffer.write(secrets.token_bytes(32))' > "$staging/publication-kek"
    "$release/venv/bin/python" -c 'import secrets, sys; sys.stdout.buffer.write(secrets.token_bytes(32))' > "$staging/publication-signing-key"
    application_secret_version=$(application_secret_kek_version)
    generate_application_secret_keyring "$release" "$staging/application-secret-kek-ring.json" "$application_secret_version"

    derive_public_key "$release" "$staging/publication-signing-key" "$staging/publication-signing-public-key"

    for f in publication-kek publication-signing-key publication-signing-public-key; do
        [[ $(wc -c < "$staging/$f") -eq 32 ]] || die "generated $f is not 32 bytes"
    done
    for f in database-owner-password backup-role-password; do
        [[ $(read_secret "$staging/$f") =~ ^[0-9a-f]{64}$ ]] || die "generated $f is not 64 hex characters"
    done

    local rederived
    rederived="$staging/.rederived-pub"
    derive_public_key "$release" "$staging/publication-signing-key" "$rederived"
    cmp -s "$staging/publication-signing-public-key" "$rederived" || die "public key derivation mismatch"
    rm -f "$rederived"

    install -d -m 0700 -o root -g root "$SECRET_DIR"
    for f in database-owner-password backup-role-password publication-kek publication-signing-key publication-signing-public-key application-secret-kek-ring.json; do
        install -m 0600 -o root -g root "$staging/$f" "$SECRET_DIR/$f"
    done
    ensure_application_secret_keyring "$release"
    render_env "$runtime_password" "$secret_key" "$host"
    ensure_public_signing_key_ring "$release"

    : > "$SECRETS_MARKER"
    chmod 600 "$SECRETS_MARKER"
    chown root:root "$SECRETS_MARKER"

    rm -rf "$staging"
}

# Validate an established install's secrets.  Upgrades from before
# application-secret encryption have no ring yet, so provision exactly that
# missing credential while preserving every existing deployment secret.
validate_established_secrets() {
    local release=$1 f v rederived
    for f in publication-kek publication-signing-key publication-signing-public-key; do
        [[ -f $SECRET_DIR/$f ]] || die "established install: $SECRET_DIR/$f is missing (restore from backup)"
        [[ $(wc -c < "$SECRET_DIR/$f") -eq 32 ]] || die "established install: $f must be exactly 32 bytes"
    done
    ensure_application_secret_keyring "$release"
    for f in database-owner-password backup-role-password; do
        [[ -f $SECRET_DIR/$f ]] || die "established install: $SECRET_DIR/$f is missing (restore from backup)"
        v=$(read_secret "$SECRET_DIR/$f")
        [[ $v =~ ^[0-9a-f]{64}$ ]] || die "established install: $f is not 64 hex characters"
    done
    rederived=$(mktemp)
    derive_public_key "$release" "$SECRET_DIR/publication-signing-key" "$rederived"
    cmp -s "$SECRET_DIR/publication-signing-public-key" "$rederived" \
        || die "established install: publication public key does not match the private seed"
    rm -f "$rederived"
    [[ -f $ENV_FILE ]] || die "established install: $ENV_FILE is missing"
    v=$(env_value "$ENV_FILE" POSTGRES_PASSWORD)
    [[ $v =~ ^[0-9a-f]{64}$ ]] || die "established install: POSTGRES_PASSWORD is invalid"
    v=$(env_value "$ENV_FILE" DJANGO_SECRET_KEY)
    [[ -n $v ]] || die "established install: DJANGO_SECRET_KEY is missing"
    ensure_public_signing_key_ring "$release"
}

# Read-only validation before maintenance. Never provision or replace a key here.
validate_premaintenance_secrets() {
    local release=$1 f rederived version
    for f in publication-kek publication-signing-key publication-signing-public-key; do
        [[ -f $SECRET_DIR/$f && $(wc -c < "$SECRET_DIR/$f") -eq 32 ]] \
            || die "publication credentials: missing or invalid; restore protected backup before retrying"
    done
    for f in database-owner-password backup-role-password; do
        [[ $(read_secret "$SECRET_DIR/$f") =~ ^[0-9a-f]{64}$ ]] \
            || die "database credentials: missing or invalid; restore protected backup before retrying"
    done
    [[ $(env_value "$ENV_FILE" POSTGRES_PASSWORD) =~ ^[0-9a-f]{64}$ ]] \
        || die "POSTGRES_PASSWORD: invalid existing runtime password"
    rederived=$(mktemp)
    chmod 0600 "$rederived"
    if ! derive_public_key "$release" "$SECRET_DIR/publication-signing-key" "$rederived"; then
        rm -f "$rederived"
        die "publication signing credentials: could not validate key pair"
    fi
    if ! cmp -s "$SECRET_DIR/publication-signing-public-key" "$rederived"; then
        rm -f "$rederived"
        die "publication signing credentials: key pair mismatch"
    fi
    rm -f "$rederived"
    ensure_public_signing_key_ring "$release" validate-only >/dev/null 2>&1 \
        || die "publication public keyring: invalid; restore or repair protected credential before retrying"
    version=$(application_secret_kek_version)
    validate_application_secret_keyring "$release" "$SECRET_DIR/application-secret-kek-ring.json" "$version" >/dev/null 2>&1 \
        || die "application-secret keyring: restore or repair protected credential before retrying"
    version=$(report_transfer_active_key)
    validate_report_transfer_keyring "$release" "$SECRET_DIR/report-transfer-keyring.json" "$version" >/dev/null 2>&1 \
        || die "Report Transfer keyring: restore or repair protected credential before retrying"
}

# Main entry. Requires FIREDASH_STATE, FIREDASH_RELEASE, FIREDASH_HOST from the caller.
secrets_ensure() {
    local state=${FIREDASH_STATE:-PRISTINE} release=${FIREDASH_RELEASE:?} host=${FIREDASH_HOST:?} proxy_url
    if [[ -f $ENV_FILE ]]; then
        local configured_root configured_path
        configured_root=$(env_value "$ENV_FILE" REPORT_TRANSFER_ROOT)
        configured_path=$(env_value "$ENV_FILE" REPORT_TRANSFER_KEYRING_PATH)
        [[ -z $configured_root || $configured_root == /var/spool/firedash-reports ]] \
            || die "custom REPORT_TRANSFER_ROOT requires explicit spool migration review; installer will not move or abandon ciphertext"
        [[ -z $configured_path ]] \
            || die "remove direct REPORT_TRANSFER_KEYRING_PATH override only after restoring the canonical credential; installer requires systemd LoadCredential"
    fi
    proxy_url=$(effective_outbound_mail_https_proxy)
    validate_outbound_mail_https_proxy "$release" "$proxy_url" \
        || die "OUTBOUND_MAIL_HTTPS_PROXY must be empty or an http:// or https:// proxy URL with a hostname"
    if [[ $state == ESTABLISHED ]]; then
        log "validating established secrets"
        validate_established_secrets "$release"
        render_env "" "" "$host"
    else
        log "generating first-install secret set"
        generate_and_commit_secrets "$release" "$host"
    fi
    ensure_report_transfer_keyring "$release"
}
