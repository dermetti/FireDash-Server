#!/usr/bin/env bash
# Root-only rotation helper regression test. Run on a Linux host with Bash/Python.
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ETC="$TMP/etc/fire-backend"
CREDS="$ETC/credentials"
RELEASE="$TMP/release"
mkdir -p "$CREDS" "$RELEASE/venv/bin" "$TMP/bin"
ln -s "$(command -v python3)" "$RELEASE/venv/bin/python"
export FIREDASH_ETC="$ETC" FIREDASH_RELEASE="$RELEASE" FIREDASH_CURRENT_LINK="$TMP/current"
export PATH="$TMP/bin:$PATH"

cat > "$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 755 "$TMP/bin/systemctl"

python3 - "$CREDS" <<'PY'
import base64, json, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
root = Path(sys.argv[1])
private = Ed25519PrivateKey.generate()
seed = private.private_bytes_raw()
public = private.public_key().public_bytes_raw()
(root / "publication-signing-key").write_bytes(seed)
(root / "publication-signing-public-key").write_bytes(public)
(root / "publication-signing-public-key-ring.json").write_text(json.dumps({"keys": {"1": base64.b64encode(public).decode()}}), encoding="ascii")
PY
cat > "$ETC/fire-backend.env" <<'EOF'
POSTGRES_PASSWORD=not-used
DJANGO_SECRET_KEY=not-used
PUBLICATION_SIGNING_KEY_VERSION=1
EOF
chmod 700 "$CREDS"
chmod 600 "$CREDS"/*
chmod 640 "$ETC/fire-backend.env"

HELPER="$ROOT/deploy/rotate-publication-signing-key"
bash "$HELPER" prepare --version 2 >/dev/null
python3 - "$CREDS/publication-signing-public-key-ring.json" <<'PY'
import json, sys
keys = json.load(open(sys.argv[1]))["keys"]
assert set(keys) == {"1", "2"}
PY
if bash "$HELPER" prepare --version 2 >/dev/null 2>&1; then
    echo "duplicate prepare unexpectedly succeeded" >&2; exit 1
fi

# A staged key that no longer matches its ring entry must not activate.
printf x > "$CREDS/publication-signing-key-staging/2/public"
if bash "$HELPER" activate --version 2 >/dev/null 2>&1; then
    echo "mismatched staged key unexpectedly activated" >&2; exit 1
fi
grep -qx 'PUBLICATION_SIGNING_KEY_VERSION=1' "$ETC/fire-backend.env"
python3 - "$CREDS/publication-signing-key-staging/2/private" "$CREDS/publication-signing-key-staging/2/public" <<'PY'
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
private, public = sys.argv[1:3]
seed = open(private, "rb").read()
open(public, "wb").write(Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw())
PY
bash "$HELPER" activate --version 2 >/dev/null
grep -qx 'PUBLICATION_SIGNING_KEY_VERSION=2' "$ETC/fire-backend.env"
status=$(bash "$HELPER" status)
grep -q 'active public matches ring: yes' <<<"$status"
grep -q 'public version 1:' <<<"$status"
grep -q 'public version 2:' <<<"$status"
if grep -Fq "$(base64 -w0 "$CREDS/publication-signing-key")" <<<"$status"; then
    echo "status exposed private signing material" >&2; exit 1
fi
echo "publication signing-key rotation helper tests passed"
