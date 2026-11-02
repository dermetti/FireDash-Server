#!/usr/bin/env bash
# Called by verify-deployment.sh with its fail()/ok() accumulator and context.
# All unit properties below are effective systemd values, including overrides.
verify_report_transfer() {
    local credential="$SECRET_DIR/report-transfer-keyring.json" version pid unit value nginx_block
    version=$(report_transfer_active_key) || { fail "invalid Report Transfer active version"; return; }
    if [[ ! -L $credential && -f $credential ]] \
        && [[ $(stat -c '%U:%G:%a' "$credential") == root:root:600 ]] \
        && validate_report_transfer_keyring "$RELEASE" "$credential" "$version"; then
        ok "Report Transfer RSA keyring and active public key valid"
    else
        fail "Report Transfer keyring missing, invalid or not root:root 0600; restore retained credentials"
    fi

    value=$(systemctl show -p LoadCredential --value fire-backend.service) || value=""
    [[ " $value " == *" report-transfer-keyring:/etc/fire-backend/credentials/report-transfer-keyring.json "* ]] \
        && ok "web loads canonical report credential" || fail "web missing canonical report LoadCredential"
    value=$(systemctl show -p LoadCredentialEncrypted --value fire-backend.service) || value=report-transfer-keyring
    [[ $value != *report-transfer-keyring* ]] \
        || fail "web has conflicting encrypted report credential override"
    [[ $(systemctl show -p User --value fire-backend.service) == fire_backend \
        && $(systemctl show -p Group --value fire-backend.service) == fire_backend ]] \
        && ok "web report identity" || fail "web must run as fire_backend"
    value=$(systemctl show -p ReadWritePaths --value fire-backend.service) || value=""
    [[ " $value " == *" /var/spool/firedash-reports "* && " $value " == *" /var/lib/fire-backend "* ]] \
        && ok "web report spool write boundary" || fail "web ReadWritePaths missing report spool or existing application data"
    validate_report_spool && ok "private report spool fire_backend:fire_backend 0700" \
        || fail "unsafe or missing report spool"
    runuser -u fire_backend -- test -r "$REPORT_SPOOL" \
        && runuser -u fire_backend -- test -w "$REPORT_SPOOL" \
        && runuser -u fire_backend -- test -x "$REPORT_SPOOL" \
        && ok "backend can use report spool" || fail "backend cannot use report spool"
    for unit in fire_publication fire_pdf_sanitizer www-data; do
        if ! id "$unit" >/dev/null 2>&1; then
            fail "cannot verify report isolation: service identity $unit missing"
        elif runuser -u "$unit" -- test -r "$REPORT_SPOOL" \
            || runuser -u "$unit" -- test -w "$REPORT_SPOOL" \
            || runuser -u "$unit" -- test -x "$REPORT_SPOOL"; then
            fail "$unit can access private report spool"
        else
            ok "$unit denied report spool access"
        fi
    done

    for unit in fire-report-expiry.service fire-report-expiry.timer; do
        [[ $(systemctl show -p LoadState --value "$unit") == loaded ]] \
            && ok "$unit installed" || fail "$unit not installed"
    done
    systemctl is-enabled --quiet fire-report-expiry.timer \
        && systemctl is-active --quiet fire-report-expiry.timer \
        && ok "report expiry timer enabled and active" || fail "report expiry timer disabled or inactive"
    [[ $(systemctl show -p User --value fire-report-expiry.service) == fire_backend \
        && $(systemctl show -p Group --value fire-report-expiry.service) == fire_backend \
        && $(systemctl show -p WorkingDirectory --value fire-report-expiry.service) == /srv/firedash/current \
        && $(systemctl show -p ProtectSystem --value fire-report-expiry.service) == strict \
        && $(systemctl show -p NoNewPrivileges --value fire-report-expiry.service) == yes \
        && $(systemctl show -p ReadWritePaths --value fire-report-expiry.service) == /var/spool/firedash-reports ]] \
        && ok "report expiry identity and narrow write boundary" || fail "report expiry identity/sandbox unexpected"
    value=$(systemctl show -p ExecStart --value fire-report-expiry.service) || value=""
    [[ $value == *'argv[]=/srv/firedash/current/venv/bin/python manage.py expire_report_submissions ;'* ]] \
        && ok "report expiry command" || fail "report expiry command unexpected"
    value=$(systemctl show -p Environment --value fire-report-expiry.service) || value=""
    [[ " $value " == *' DJANGO_SETTINGS_MODULE=config.settings.production '* ]] \
        && [[ $(systemctl show -p EnvironmentFiles --value fire-report-expiry.service) == '/etc/fire-backend/fire-backend.env (ignore_errors=no)' ]] \
        && ok "report expiry production environment" || fail "report expiry runtime environment unexpected"
    value=$(systemctl show -p TimersMonotonic --value fire-report-expiry.timer) || value=""
    [[ $value == *'OnBootUSec=5min'* && $value == *'OnUnitActiveUSec=5min'* \
        && $(systemctl show -p Unit --value fire-report-expiry.timer) == fire-report-expiry.service \
        && $(systemctl show -p Persistent --value fire-report-expiry.timer) == yes ]] \
        && ok "report expiry five-minute schedule" || fail "report expiry schedule unexpected"
    for unit in fire-report-expiry.service fire-publication-build.service fire-publication-delivery.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
        value=$(systemctl show -p LoadCredential -p LoadCredentialEncrypted --value "$unit") || { fail "cannot inspect $unit credentials"; continue; }
        if [[ $value == *report-transfer-keyring* ]]; then
            fail "$unit unnecessarily receives report RSA keyring"
        fi
    done

    nginx_block=$(awk '
        $1 == "location" && $2 == "^~" && $3 == "/reports/access/" { copying=1; next }
        copying && $1 == "}" { exit }
        copying { print }
    ' "${FIREDASH_NGINX_SITE:-/etc/nginx/sites-available/fire-backend}")
    grep -Eq '^[[:space:]]*access_log off;' <<<"$nginx_block" \
        && grep -Eq '^[[:space:]]*error_log /dev/null crit;' <<<"$nginx_block" \
        && ok "Nginx suppresses bearer route logs" || fail "Nginx report token logging suppression missing"
    value=$(env_value "$ENV_FILE" FIREDASH_PUBLIC_ORIGIN)
    [[ $value == "https://$HOST" ]] && normalize_base_url "$value" >/dev/null \
        && ok "report links use canonical HTTPS origin" || fail "Report Transfer public HTTPS origin unexpected"

    pid=$(systemctl show -p MainPID --value fire-backend.service) || pid=0
    if [[ $pid =~ ^[1-9][0-9]*$ ]] \
        && PYTHONPATH="$RELEASE" "$RELEASE/venv/bin/python" \
            "$RELEASE/deploy/scripts/report-transfer-credential.py" probe "$pid" "$RELEASE" "$version" "https://$HOST"; then
        ok "running web namespace loads report keyring and advertises enabled JWE/JPEG configuration"
    else
        fail "running web Report Transfer configuration unavailable; inspect credential mount, active version and service sandbox"
    fi
}
