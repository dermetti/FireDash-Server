#!/usr/bin/env bash
# Called by verify-deployment.sh with its fail()/ok() accumulator and context.
# Complex D-Bus properties may be [unprintable] on Debian. Inspect effective
# fragments/drop-ins only when systemd confirms no pending daemon reload. The
# running-process probe below independently checks the credential mount.

report_unit_property() {
    local property=$1 unit=$2 value
    value=$(systemctl show -p "$property" --value "$unit") || return 1
    # Accept both --value and property-prefixed representations, never empty.
    value=${value#"$property="}
    [[ -n $value && $value != '[unprintable]' ]] || return 1
    printf '%s' "$value"
}

report_unit_configuration() {
    local unit=$1 mode=$2 configuration
    [[ $(report_unit_property NeedDaemonReload "$unit") == no ]] || return 1
    configuration=$(systemctl cat -- "$unit") || return 1
    [[ -n $configuration ]] || return 1
    # No cat output (which may include administrator secrets) reaches logs.
    printf '%s\n' "$configuration" | "$RELEASE/venv/bin/python" \
        "$RELEASE/deploy/scripts/report-transfer-credential.py" inspect-unit "$mode"
}

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

    report_unit_configuration fire-backend.service web \
        && ok "web canonical report credential and spool write boundary" \
        || fail "web report unit configuration missing/conflicting or daemon reload pending"
    [[ $(report_unit_property User fire-backend.service) == fire_backend \
        && $(report_unit_property Group fire-backend.service) == fire_backend ]] \
        && ok "web report identity" || fail "web must run as fire_backend"
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
        [[ $(report_unit_property LoadState "$unit") == loaded ]] \
            && ok "$unit installed" || fail "$unit not installed"
    done
    systemctl is-enabled --quiet fire-report-expiry.timer \
        && systemctl is-active --quiet fire-report-expiry.timer \
        && ok "report expiry timer enabled and active" || fail "report expiry timer disabled or inactive"
    [[ $(report_unit_property User fire-report-expiry.service) == fire_backend \
        && $(report_unit_property Group fire-report-expiry.service) == fire_backend \
        && $(report_unit_property WorkingDirectory fire-report-expiry.service) == /srv/firedash/current \
        && $(report_unit_property ProtectSystem fire-report-expiry.service) == strict \
        && $(report_unit_property NoNewPrivileges fire-report-expiry.service) == yes ]] \
        && ok "report expiry identity and narrow write boundary" || fail "report expiry identity/sandbox unexpected"
    report_unit_configuration fire-report-expiry.service expiry \
        && ok "report expiry command, environment and narrow write boundary" \
        || fail "report expiry effective configuration unexpected or daemon reload pending"
    report_unit_configuration fire-report-expiry.timer timer \
        && ok "report expiry five-minute schedule" \
        || fail "report expiry schedule unexpected or daemon reload pending"
    for unit in fire-publication-build.service fire-publication-delivery.service fire-publication-maintenance.service fire-import-staging-maintenance.service fire-temporary-assignment-expiry.service fire-stale-installation.service; do
        report_unit_configuration "$unit" worker \
            || fail "cannot confirm $unit excludes report RSA keyring (check unit configuration/daemon reload)"
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

    pid=$(report_unit_property MainPID fire-backend.service) || pid=0
    if [[ $pid =~ ^[1-9][0-9]*$ ]] \
        && PYTHONPATH="$RELEASE" "$RELEASE/venv/bin/python" \
            "$RELEASE/deploy/scripts/report-transfer-credential.py" probe "$pid" "$RELEASE" "$version" "https://$HOST"; then
        ok "running web namespace loads report keyring and advertises enabled JWE/JPEG configuration"
    else
        fail "running web Report Transfer configuration unavailable; inspect credential mount, active version and service sandbox"
    fi
}
