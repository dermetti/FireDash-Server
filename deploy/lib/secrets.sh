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

# Render /etc/fire-backend/fire-backend.env. Empty runtime_password/secret_key reuse existing values.
render_env() {
    local runtime_password=${1:-} secret_key=${2:-} host=${3:-} signing_key_version=1
    if [[ -f $ENV_FILE ]]; then
        [[ -z $runtime_password ]] && runtime_password=$(env_value "$ENV_FILE" POSTGRES_PASSWORD)
        [[ -z $secret_key ]] && secret_key=$(env_value "$ENV_FILE" DJANGO_SECRET_KEY)
        signing_key_version=$(env_value "$ENV_FILE" PUBLICATION_SIGNING_KEY_VERSION)
    fi
    [[ -n $runtime_password ]] || die "runtime database password is unavailable"
    [[ -n $secret_key ]] || die "Django SECRET_KEY is unavailable"
    [[ -n $host ]] || die "hostname is unavailable"
    [[ -n $signing_key_version ]] || signing_key_version=1
    [[ $signing_key_version =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
        || die "PUBLICATION_SIGNING_KEY_VERSION is invalid"
    local tmp
    tmp=$(mktemp)
    cat > "$tmp" <<EOF
POSTGRES_DB=fire_backend
POSTGRES_USER=application_runtime
POSTGRES_PASSWORD=$runtime_password
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
DJANGO_SECRET_KEY=$secret_key
DJANGO_ALLOWED_HOSTS=$host
DJANGO_CSRF_TRUSTED_ORIGINS=https://$host
FIREDASH_PUBLIC_ORIGIN=https://$host
DJANGO_STATIC_ROOT=/var/lib/fire-backend/static
ADMIN_SESSION_MAX_AGE_SECONDS=28800
PRE_MFA_SESSION_MAX_AGE_SECONDS=600
AUTH_THROTTLE_MAX_FAILURES=5
AUTH_THROTTLE_WINDOW_SECONDS=900
AUTH_THROTTLE_LOCKOUT_SECONDS=900
RECENT_REAUTH_MAX_AGE_SECONDS=900
TRUSTED_PROXY_IPS=127.0.0.1,::1
REFERENCE_DATA_QUARANTINE_ROOT=/var/lib/fire-backend/quarantine
REFERENCE_DATA_SANITIZER_OUTPUT_ROOT=/var/lib/fire-backend/sanitizer-output
REFERENCE_DATA_ACCEPTED_ROOT=/var/lib/fire-backend/fire-plans
MAX_PDF_INPUT_BYTES=104857600
MAX_PDF_OUTPUT_BYTES=157286400
MAX_PDF_PAGES=500
MAX_HYDRANT_IMPORT_FEATURES=20000
PDF_SANITIZER_TIMEOUT_SECONDS=60
PDF_SANITIZER_MEMORY_MAX_BYTES=536870912
PDF_SANITIZER_BROKER_SOCKET=/run/fire-pdf-sanitizer-broker/broker.sock
PUBLICATION_WORKER_BATCH_SIZE=10
PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS=300
PUBLICATION_JOB_MAX_ATTEMPTS=3
TEMPORARY_ASSIGNMENT_EXPIRY_BATCH_SIZE=100
PUBLICATION_BUILD_SUMMARY_MAX_ITEMS=10000
SIGNED_MANIFEST_RETENTION_DAYS=30
PUBLICATION_ARTIFACT_ROOT=/var/lib/fire-backend/publications
PUBLICATION_ARTIFACT_TEMP_ROOT=/var/lib/fire-backend/publications/.tmp
PUBLICATION_ARTIFACT_MAX_BYTES=104857600
PUBLICATION_ARTIFACT_STALE_SECONDS=3600
PUBLICATION_KEK_VERSION=1
PUBLICATION_SIGNING_KEY_VERSION=$signing_key_version
EOF
    install_file_atomic "$tmp" "$ENV_FILE" 0640 root:fire_backend
    rm -f "$tmp"
}

# Preserve every historical public key while ensuring the active private/public
# pair is represented by its configured immutable version.  The ring is loaded
# as a public-only systemd credential by web and publication services.
ensure_public_signing_key_ring() {
    local release=$1 active_version tmp
    active_version=$(env_value "$ENV_FILE" PUBLICATION_SIGNING_KEY_VERSION)
    [[ -n $active_version ]] || active_version=1
    tmp=$(mktemp)
    "$release/venv/bin/python" - "$SECRET_DIR/publication-signing-public-key" \
        "$SECRET_DIR/publication-signing-public-key-ring.json" "$active_version" > "$tmp" <<'PY'
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
keys[active_version] = active_encoded
sys.stdout.write(json.dumps({"keys": keys}, sort_keys=True, separators=(",", ":")) + "\n")
PY
    install_file_atomic "$tmp" "$SECRET_DIR/publication-signing-public-key-ring.json" 0600 root:root
    rm -f "$tmp"
}

# Generate and commit a full secret set. Only safe in PRISTINE / BOOTSTRAP_INCOMPLETE.
generate_and_commit_secrets() {
    local release=$1 host=$2 staging runtime_password secret_key f
    rm -rf /etc/fire-backend/.secrets-staging.*
    staging=$(mktemp -d /etc/fire-backend/.secrets-staging.XXXXXX)
    chmod 700 "$staging"

    openssl rand -hex 32 > "$staging/database-owner-password"
    openssl rand -hex 32 > "$staging/backup-role-password"
    runtime_password=$(openssl rand -hex 32)
    secret_key=$("$release/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(64))')

    "$release/venv/bin/python" -c 'import secrets, sys; sys.stdout.buffer.write(secrets.token_bytes(32))' > "$staging/publication-kek"
    "$release/venv/bin/python" -c 'import secrets, sys; sys.stdout.buffer.write(secrets.token_bytes(32))' > "$staging/publication-signing-key"

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
    for f in database-owner-password backup-role-password publication-kek publication-signing-key publication-signing-public-key; do
        install -m 0600 -o root -g root "$staging/$f" "$SECRET_DIR/$f"
    done
    render_env "$runtime_password" "$secret_key" "$host"
    ensure_public_signing_key_ring "$release"

    : > "$SECRETS_MARKER"
    chmod 600 "$SECRETS_MARKER"
    chown root:root "$SECRETS_MARKER"

    rm -rf "$staging"
}

# Validate an established install's secrets. Fails closed; never regenerates.
validate_established_secrets() {
    local release=$1 f v rederived
    for f in publication-kek publication-signing-key publication-signing-public-key; do
        [[ -f $SECRET_DIR/$f ]] || die "established install: $SECRET_DIR/$f is missing (restore from backup)"
        [[ $(wc -c < "$SECRET_DIR/$f") -eq 32 ]] || die "established install: $f must be exactly 32 bytes"
    done
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

# Main entry. Requires FIREDASH_STATE, FIREDASH_RELEASE, FIREDASH_HOST from the caller.
secrets_ensure() {
    local state=${FIREDASH_STATE:-PRISTINE} release=${FIREDASH_RELEASE:?} host=${FIREDASH_HOST:?}
    if [[ $state == ESTABLISHED ]]; then
        log "validating established secrets"
        validate_established_secrets "$release"
        render_env "" "" "$host"
    else
        log "generating first-install secret set"
        generate_and_commit_secrets "$release" "$host"
    fi
}
