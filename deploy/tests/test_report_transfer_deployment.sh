#!/usr/bin/env bash
# Disposable real RSA/parser tests plus modeled OS/systemd boundaries. No host writes.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export FIREDASH_ETC="$TMP/etc" FIREDASH_REPORT_SPOOL="$TMP/spool"
export FIREDASH_SYSTEMD_UNIT_DIR="$TMP/systemd" PYTHONPATH="$ROOT"
export FIREDASH_TEST_PYTHON=${FIREDASH_TEST_PYTHON:-python3}
export BAD="" FAIL_PROBE=""
RELEASE="$TMP/release"
mkdir -p "$FIREDASH_ETC/credentials" "$RELEASE/venv/bin" "$RELEASE/deploy/scripts"
cp "$ROOT/deploy/scripts/report-transfer-credential.py" "$RELEASE/deploy/scripts/"
cat > "$RELEASE/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
if [[ ${2:-} == probe ]]; then [[ -z $FAIL_PROBE ]]; exit; fi
exec "$FIREDASH_TEST_PYTHON" "$@"
EOF
chmod +x "$RELEASE/venv/bin/python"
source "$ROOT/deploy/lib/secrets.sh"
source "$ROOT/deploy/lib/report-transfer.sh"
source "$ROOT/deploy/lib/systemd.sh"
source "$ROOT/deploy/lib/verify-report-transfer.sh"

# No production identities in this sandbox. Only ownership/access are modeled;
# RSA, JSON parsing, file bytes, environment rendering and DB probe decisions run.
stat() {
    if [[ $BAD == permissions ]]; then printf 'root:root:644'; return; fi
    case ${*: -1} in
        "$REPORT_SPOOL") printf 'fire_backend:fire_backend:700' ;;
        "$SECRET_DIR") printf 'root:root:700' ;;
        *) printf 'root:root:600' ;;
    esac
}
install() {
    [[ $1 == -d ]] || return 1
    [[ $* == "-d -o fire_backend -g fire_backend -m 0700 $REPORT_SPOOL" ]]
    mkdir -- "${@: -1}"
}
install_file_atomic() {
    local tmp
    tmp=$(mktemp "${2}.tmp.XXXXXX")
    cat "$1" > "$tmp"
    chmod "$3" "$tmp"
    mv "$tmp" "$2"
}
write_string_atomic() {
    printf '%s\n' "$1" > "$2"
    chmod "$3" "$2"
}
DB=absent
as_postgres() {
    [[ $DB != unknown ]] || return 1
    case ${*: -1} in
        *pg_database*) [[ $DB == absent ]] && echo 0 || echo 1 ;;
        *"to_regclass('public.report_transfer_reportsubmission')"*) [[ $DB == prefeature ]] && echo f || echo t ;;
        *"to_regclass('public.django_migrations')"*) echo t ;;
        *django_migrations*) echo f ;;
        *'EXISTS (SELECT 1 FROM public.report_transfer_reportsubmission)'*) [[ $DB == records ]] && echo t || echo f ;;
        *) return 1 ;;
    esac
}
expect_failure() {
    if ("$@") > "$TMP/error" 2>&1; then echo "FAIL: accepted $*" >&2; exit 1; fi
}
credential="$SECRET_DIR/report-transfer-keyring.json"
ensure_report_spool
[[ $(report_transfer_active_key) == 1 ]]
printf 'REPORT_TRANSFER_ACTIVE_KEY=release-2\n' > "$ENV_FILE"
ensure_report_transfer_keyring "$RELEASE"
validate_report_transfer_keyring "$RELEASE" "$credential" release-2
cp "$credential" "$TMP/original"
ensure_report_transfer_keyring "$RELEASE"
cmp "$credential" "$TMP/original"
# Preserve active environment selection on upgrade, rather than resetting to 1.
render_env fixture-password fixture-secret firedash.example.test
grep -qx 'REPORT_TRANSFER_ACTIVE_KEY=release-2' "$ENV_FILE"

# Keep historical key versions. Real runtime parser must accept all entries.
"$RELEASE/venv/bin/python" - "$credential" <<'PY'
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
doc = json.loads(p.read_text())
doc["keys"]["1"] = doc["keys"]["release-2"]
p.write_text(json.dumps(doc))
PY
cp "$credential" "$TMP/multi"
ensure_report_transfer_keyring "$RELEASE"
cmp "$credential" "$TMP/multi"

for bad in json pem weak wrong-type duplicate missing-active; do
    "$RELEASE/venv/bin/python" - "$credential" "$bad" <<'PY'
import json
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec
path, kind = Path(sys.argv[1]), sys.argv[2]
if kind in ("weak", "wrong-type"):
    key = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
           if kind == "weak" else ec.generate_private_key(ec.SECP256R1()))
    value = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")
    doc = json.dumps({"keys": {"release-2": value}})
else:
    doc = {"json": "{", "pem": '{"keys":{"release-2":"bad"}}',
           "duplicate": '{"keys":{},"keys":{}}', "missing-active": '{"keys":{}}'}[kind]
path.write_text(doc)
PY
    cp "$credential" "$TMP/bad-before"
    expect_failure ensure_report_transfer_keyring "$RELEASE"
    cmp "$credential" "$TMP/bad-before"
done
cp "$TMP/multi" "$credential"
BAD=permissions
expect_failure ensure_report_transfer_keyring "$RELEASE"
BAD=""
rm "$credential"
expect_failure ensure_report_transfer_keyring "$RELEASE" # initialized marker
rm "$FIREDASH_ETC/report-transfer-initialized"
for DB in records unknown; do expect_failure ensure_report_transfer_keyring "$RELEASE"; done
DB=absent
printf ciphertext > "$REPORT_SPOOL/payload.jwe"
printf receipt > "$REPORT_SPOOL/receipt.json"
expect_failure ensure_report_transfer_keyring "$RELEASE"
ensure_report_spool
[[ $(cat "$REPORT_SPOOL/payload.jwe") == ciphertext && $(cat "$REPORT_SPOOL/receipt.json") == receipt ]]
BAD=permissions
expect_failure ensure_report_spool
BAD=""
# Move only these disposable test files; no installer data deletion is exercised.
mv "$REPORT_SPOOL/payload.jwe" "$TMP/payload.saved"
mv "$REPORT_SPOOL/receipt.json" "$TMP/receipt.saved"
DB=prefeature
ensure_report_transfer_keyring "$RELEASE"
cp "$credential" "$TMP/restored"
mv "$REPORT_SPOOL" "$TMP/saved-spool"
printf not-directory > "$REPORT_SPOOL"
expect_failure ensure_report_spool
rm "$REPORT_SPOOL"
mv "$TMP/saved-spool" "$REPORT_SPOOL"
if [[ $(uname -s) == Linux ]]; then
    mv "$REPORT_SPOOL" "$TMP/link-target"
    ln -s "$TMP/link-target" "$REPORT_SPOOL"
    expect_failure ensure_report_spool
    rm "$REPORT_SPOOL"
    mv "$TMP/link-target" "$REPORT_SPOOL"
else
    echo 'SKIP: native Unix symlink rejection check requires Linux'
fi

if [[ $(uname -s) == Linux && $EUID == 0 ]] \
    && id fire_backend >/dev/null 2>&1 && id fire_publication >/dev/null 2>&1 \
    && id fire_pdf_sanitizer >/dev/null 2>&1 && id www-data >/dev/null 2>&1; then
    (
        # No account creation or host directories: use existing test-container
        # identities against this disposable tree, with the real Unix helpers.
        unset -f stat install
        chmod 0755 "$TMP"
        REPORT_SPOOL="$TMP/native-spool"
        ensure_report_spool
        [[ $(command stat -c '%U:%G:%a' "$REPORT_SPOOL") == fire_backend:fire_backend:700 ]]
        runuser -u fire_backend -- sh -c 'umask 077; printf ciphertext > "$1/payload"; printf receipt > "$1/receipt"' sh "$REPORT_SPOOL"
        ensure_report_spool
        [[ $(cat "$REPORT_SPOOL/payload") == ciphertext && $(cat "$REPORT_SPOOL/receipt") == receipt ]]
        for identity in fire_publication fire_pdf_sanitizer www-data; do
            ! runuser -u "$identity" -- test -x "$REPORT_SPOOL"
            ! runuser -u "$identity" -- test -r "$REPORT_SPOOL"
            ! runuser -u "$identity" -- test -w "$REPORT_SPOOL"
        done
        chmod 0755 "$REPORT_SPOOL"
        expect_failure ensure_report_spool
    )
else
    echo 'SKIP: real ownership/access test needs root-capable isolated Linux with service identities'
fi

# Only the exact historical override is removed; mixed admin settings survive.
mkdir -p "$FIREDASH_SYSTEMD_UNIT_DIR/fire-backend.service.d"
dropin="$FIREDASH_SYSTEMD_UNIT_DIR/fire-backend.service.d/report-transfer.conf"
printf '%s\n' '[Service]' \
    'LoadCredential=report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json' \
    'ReadWritePaths=/var/spool/firedash-reports' > "$dropin"
reconcile_report_transfer_dropins
[[ ! -e $dropin ]]
printf '[Service]\nMemoryMax=1G\n' > "$dropin"
reconcile_report_transfer_dropins
[[ -f $dropin ]]
printf 'ReadWritePaths=/var/spool/firedash-reports\n' >> "$dropin"
cp "$dropin" "$TMP/admin-before"
expect_failure reconcile_report_transfer_dropins
cmp "$dropin" "$TMP/admin-before"

echo 'ok: RSA generation/parser, active/history preservation, fail-closed upgrades, private spool and override reconciliation'

# Inspect real canonical units and realistic drop-in semantics without systemd.
"$RELEASE/venv/bin/python" - "$ROOT" "$RELEASE/deploy/scripts/report-transfer-credential.py" <<'PY'
import runpy
import sys
from pathlib import Path
root = Path(sys.argv[1])
inspect = runpy.run_path(sys.argv[2])["inspect_unit"]
web = (root / "deploy/systemd/fire-backend.service").read_text()
expiry = (root / "deploy/systemd/fire-report-expiry.service").read_text()
timer = (root / "deploy/systemd/fire-report-expiry.timer").read_text()
canonical = "report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json"
for mode, unit in (("web", web), ("expiry", expiry), ("timer", timer)):
    inspect(mode, unit)
inspect("web", web + '\n[Service]\nLoadCredential=\nLoadCredential="' + canonical + '"\n')
inspect("web", web + "\n[Service]\nReadWritePaths=\nReadWritePaths=/var/lib/fire-backend " + chr(92) + "\n /var/spool/firedash-reports\n")
inspect("expiry", expiry.replace("manage.py expire_report_submissions", '"manage.py" "expire_report_submissions"'))
inspect("timer", timer.replace("5min", "300s") + "\n[Timer]\nPersistent=yes\n")
invalid = [
    ("web", ""),
    ("web", web + "\n[Service]\nLoadCredential=\n"),
    ("web", web + "\n[Service]\nLoadCredential=" + canonical + "\n"),
    ("web", web + "\n[Service]\nLoadCredential=report-transfer-keyring:/wrong\n"),
    ("web", web + "\n[Service]\nLoadCredentialEncrypted=report-transfer-keyring:/wrong\n"),
    ("worker", "[Service]\nLoadCredential=" + canonical),
    ("worker", "[Service]\nImportCredential=report-*"),
    ("worker", "[Service]\nSetCredential=report-transfer-keyring:private-test-value"),
    ("expiry", expiry + "\n[Service]\nReadWritePaths=/tmp\n"),
    ("expiry", expiry + "\n[Service]\nExecStart=\nExecStart=/bin/false\n"),
    ("timer", timer + "\n[Timer]\nOnCalendar=daily\n"),
    ("timer", timer.replace("5min", "60min")),
    ("timer", timer + "\n[Timer]\nPersistent=false\n"),
]
for mode, unit in invalid:
    try:
        inspect(mode, unit)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe unit configuration accepted: " + mode)
print("ok: effective unit quoting, continuations, resets, conflicts and timer equivalence")
PY

# Exercise the actual verifier with an effective-systemd-property model.
systemctl_model() {
    local property=${3:-} unit=${*: -1}
    case $1 in
        cat)
            [[ $BAD != cat-missing ]] || return 1
            local source="$ROOT/deploy/systemd/$unit"
            [[ -f $source ]] || return 1
            case "$BAD:$unit" in
                LoadCredential:fire-backend.service) sed '/^LoadCredential=report-transfer-keyring:/d' "$source" ;;
                ReadWritePaths:fire-backend.service) sed '/^ReadWritePaths=/d' "$source" ;;
                *) cat "$source" ;;
            esac
            case "$BAD:$unit" in
                encrypted-conflict:fire-backend.service) printf '\n[Service]\nLoadCredentialEncrypted=report-transfer-keyring:/wrong\n' ;;
                wrong-credential:fire-backend.service) printf '\n[Service]\nLoadCredential=report-transfer-keyring:/wrong\n' ;;
                credential-reset:fire-backend.service) printf '\n[Service]\nLoadCredential=\n' ;;
                worker-leak:fire-report-expiry.service) printf '\n[Service]\nLoadCredential=report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json\n' ;;
                worker-import:fire-publication-build.service) printf '\n[Service]\nImportCredential=*\n' ;;
                ExecStart:fire-report-expiry.service) printf '\n[Service]\nExecStart=\nExecStart=/bin/false\n' ;;
                Environment:fire-report-expiry.service) printf '\n[Service]\nEnvironment=\n' ;;
                EnvironmentFiles:fire-report-expiry.service) printf '\n[Service]\nEnvironmentFile=\n' ;;
                TimersMonotonic:fire-report-expiry.timer) printf '\n[Timer]\nOnBootSec=\nOnUnitActiveSec=\n' ;;
                Unit:fire-report-expiry.timer) printf '\n[Timer]\nUnit=wrong.service\n' ;;
                Persistent:fire-report-expiry.timer) printf '\n[Timer]\nPersistent=false\n' ;;
            esac
            ;;
        is-enabled) [[ $BAD != disabled ]] ;;
        is-active) [[ $BAD != inactive ]] ;;
        show)
            [[ $property != "$BAD" ]] || return 1
            case $property in
                NeedDaemonReload) [[ $BAD == pending-reload ]] && echo yes || echo no ;;
                LoadCredential)
                    [[ $unit != fire-backend.service ]] || echo 'report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json'
                    [[ $BAD != worker-leak || $unit != fire-report-expiry.service ]] || echo 'report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json'
                    ;;
                LoadCredentialEncrypted) [[ $BAD != encrypted-conflict ]] || echo report-transfer-keyring ;;
                ReadWritePaths) [[ $unit == fire-backend.service ]] && echo '/var/lib/fire-backend /var/spool/firedash-reports' || echo /var/spool/firedash-reports ;;
                LoadState) echo loaded ;;
                User|Group) echo fire_backend ;;
                WorkingDirectory) echo /srv/firedash/current ;;
                ProtectSystem) echo strict ;;
                NoNewPrivileges|Persistent) echo yes ;;
                ExecStart) echo '{ argv[]=/srv/firedash/current/venv/bin/python manage.py expire_report_submissions ; }' ;;
                Environment) echo DJANGO_SETTINGS_MODULE=config.settings.production ;;
                EnvironmentFiles) echo '/etc/fire-backend/fire-backend.env (ignore_errors=no)' ;;
                TimersMonotonic) echo '{ OnBootUSec=5min ; } { OnUnitActiveUSec=5min ; }' ;;
                Unit) echo fire-report-expiry.service ;;
                MainPID) echo 123 ;;
                *) return 1 ;;
            esac ;;
        *) return 1 ;;
    esac
}
# Debian has rendered complex D-Bus properties as [unprintable]. Effective
# configuration and the running credential probe must remain authoritative.
REPRESENTATION=plain
systemctl() {
    local value property=${3:-}
    if [[ $1 != show ]]; then systemctl_model "$@"; return; fi
    value=$(systemctl_model "$@") || return 1
    case "$REPRESENTATION:$property" in
        opaque:LoadCredential|opaque:ReadWritePaths|opaque:ExecStart|opaque:TimersMonotonic) echo '[unprintable]' ;;
        empty:LoadCredential|empty:ReadWritePaths|empty:ExecStart|empty:TimersMonotonic) : ;;
        prefixed:*) printf '%s=%s\n' "$property" "$value" ;;
        *) printf '%s\n' "$value" ;;
    esac
}
id() { return 0; }
runuser() { [[ $2 == fire_backend && $BAD != access || $BAD == public-access ]]; }
HOST=firedash.example.test
export FIREDASH_NGINX_SITE="$TMP/nginx"
cp "$ROOT/deploy/nginx/fire-backend.conf" "$FIREDASH_NGINX_SITE"
FAIL=0
fail() { FAIL=$((FAIL + 1)); }
ok() { :; }
for REPRESENTATION in plain opaque empty prefixed; do
    FAIL=0
    verify_report_transfer
    [[ $FAIL == 0 ]] || { echo "FAIL: rejected supported $REPRESENTATION properties"; exit 1; }
done
REPRESENTATION=plain
for BAD in LoadCredential encrypted-conflict wrong-credential credential-reset cat-missing pending-reload worker-import ReadWritePaths LoadState disabled inactive User Group WorkingDirectory ProtectSystem NoNewPrivileges ExecStart Environment EnvironmentFiles TimersMonotonic Unit Persistent MainPID permissions access public-access worker-leak; do
    FAIL=0
    verify_report_transfer
    [[ $FAIL -gt 0 ]] || { echo "FAIL: verifier ignored $BAD"; exit 1; }
done
BAD=""
FAIL_PROBE=failed
FAIL=0
verify_report_transfer
[[ $FAIL -gt 0 ]]
FAIL_PROBE=""
mv "$credential" "$TMP/verify-key"
FAIL=0
verify_report_transfer
[[ $FAIL -gt 0 ]]
mv "$TMP/verify-key" "$credential"
mv "$REPORT_SPOOL" "$TMP/verify-spool"
FAIL=0
verify_report_transfer
[[ $FAIL -gt 0 ]]
mv "$TMP/verify-spool" "$REPORT_SPOOL"
: > "$FIREDASH_NGINX_SITE"
FAIL=0
verify_report_transfer
[[ $FAIL -gt 0 ]]
cp "$ROOT/deploy/nginx/fire-backend.conf" "$FIREDASH_NGINX_SITE"
sed -i 's|^FIREDASH_PUBLIC_ORIGIN=.*|FIREDASH_PUBLIC_ORIGIN=http://wrong.test|' "$ENV_FILE"
FAIL=0
verify_report_transfer
[[ $FAIL -gt 0 ]]
echo 'ok: missing/misconfigured mandatory Report Transfer components fail deployment verification'

# The model above checks failure propagation; these check the actual shipped
# privilege and scheduler configuration rather than a duplicate example.
web="$ROOT/deploy/systemd/fire-backend.service"
expiry="$ROOT/deploy/systemd/fire-report-expiry.service"
timer="$ROOT/deploy/systemd/fire-report-expiry.timer"
grep -qx 'LoadCredential=report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json' "$web"
grep -qx 'ReadWritePaths=/var/lib/fire-backend' "$web"
grep -qx 'ReadWritePaths=/var/spool/firedash-reports' "$web"
! grep -Eq 'LoadCredential=publication-(kek|signing-key):' "$web"
grep -qx 'ExecStart=/srv/firedash/current/venv/bin/python manage.py expire_report_submissions' "$expiry"
grep -qx 'User=fire_backend' "$expiry"
grep -qx 'Group=fire_backend' "$expiry"
grep -qx 'Environment=DJANGO_SETTINGS_MODULE=config.settings.production' "$expiry"
grep -qx 'EnvironmentFile=/etc/fire-backend/fire-backend.env' "$expiry"
grep -qx 'ReadWritePaths=/var/spool/firedash-reports' "$expiry"
grep -qx 'ProtectSystem=strict' "$expiry"
grep -qx 'NoNewPrivileges=true' "$expiry"
! grep -q '^LoadCredential' "$expiry"
grep -qx 'OnBootSec=5min' "$timer"
grep -qx 'OnUnitActiveSec=5min' "$timer"
grep -qx 'Persistent=true' "$timer"
grep -qx 'Unit=fire-report-expiry.service' "$timer"
grep -qx 'd /var/spool/firedash-reports 0700 fire_backend fire_backend -' "$ROOT/deploy/systemd/fire-backend.tmpfiles.conf"
! grep '^restic backup' "$ROOT/deploy/scripts/fire-backup" | grep -q '/var/spool'
grep '^restic backup' "$ROOT/deploy/scripts/fire-backup" | grep -q '/etc/fire-backend/credentials'
echo 'ok: canonical units retain credential separation, spool boundary, five-minute expiry and protected backup sources'
