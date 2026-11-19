"""Root-only TLS lifecycle. No Django imports or database access.

The installer already holds firedash-install.lock; the scheduled entrypoint takes
the same lock. Fresh issuance uses a separate Certbot namespace and verified
copies. Explicit DNS-Netcup adoption retains the original live/archive lineage.
"""

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import stat

# Root operations use argument lists, never a shell.
import subprocess  # nosec B404
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import configuration as config

ETC = Path("/etc/fire-backend")
WEBROOT = Path("/var/lib/fire-backend/acme-webroot")
WORK = Path("/var/lib/fire-backend/acme-work")
LOGS = Path("/var/log/fire-backend-acme")
SITE = Path("/etc/nginx/sites-available/fire-backend")
ENABLED = Path("/etc/nginx/sites-enabled/fire-backend")
HELPER = Path("/usr/local/lib/fire-backend/tls.py")
UNITS = Path("/etc/systemd/system")
LOCK = Path("/run/lock/firedash-install.lock")
CERTBOT = "/usr/bin/certbot"
SERVER = "https://acme-v02.api.letsencrypt.org/directory"
TIMER = "fire-tls-renew.timer"


class TLSError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TLSError(message)


def run(args, *, input=None):
    # Child diagnostics can contain URLs, account information or key paths.
    # Certbot retains its own root-only diagnostic logs; never relay raw output.
    with subprocess.Popen(  # nosec B603
        args,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            output, _ = process.communicate(
                input=input, timeout=2400 if "certbot" in Path(args[0]).name else 300
            )
        except BaseException:
            # A timed-out Certbot must not leave a deploy-hook child running
            # after the installer releases its lock. Kill the entire new session.
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            raise
    require(
        process.returncode == 0,
        "TLS command failed; inspect protected ACME logs / Nginx configuration",
    )
    return output


def hostname(origin):
    url = urlsplit(origin)
    host = url.hostname or ""
    require(
        url.scheme == "https"
        and url.netloc == host
        and not url.path
        and not url.query
        and not url.fragment
        and len(host) <= 253
        and "." in host
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ),
        "TLS requires an HTTPS origin with a valid lowercase public FQDN",
    )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise TLSError("TLS requires a DNS hostname, not an IP address")


def validate(cert, key, host, warning_days=30):
    cert, key = str(cert), str(key)
    public = run(["openssl", "x509", "-in", cert, "-pubkey", "-noout"])
    public = run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public)
    private_public = run(
        ["openssl", "pkey", "-in", key, "-passin", "pass:", "-pubout", "-outform", "DER"]
    )
    require(public == private_public, "TLS certificate/private key mismatch")
    result = run(["openssl", "x509", "-in", cert, "-noout", "-checkhost", host])
    # Some OpenSSL releases return zero even for a failed -checkhost test.
    require(b"does match certificate" in result, "TLS certificate hostname mismatch")
    dates = run(["openssl", "x509", "-in", cert, "-noout", "-dates"]).decode("ascii")
    dates = dict(line.split("=", 1) for line in dates.splitlines())
    start, end = (
        datetime.strptime(dates[k], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        for k in ("notBefore", "notAfter")
    )
    now = datetime.now(UTC)
    require(
        start <= now < end,
        "TLS certificate expired or not yet valid; renew through its owner, then rerun",
    )
    remaining = (end - now).total_seconds()
    if remaining < warning_days * 86400:
        print(
            "firedash warning: TLS certificate is within the configured expiry warning window",
            file=sys.stderr,
        )
    return remaining


def settings():
    config.validate_files(ETC)
    values = config.read(ETC / "install.conf")
    config.validate_install(values)
    hostname(values["FIREDASH_BASE_URL"])
    return values


def mode(values):
    return values.get("FIREDASH_TLS_MODE", "manual")


def paths() -> tuple[Path, Path]:
    return ETC / "tls/current/fullchain.pem", ETC / "tls/current/privkey.pem"


def adopted(values):
    return bool(values and values.get("FIREDASH_TLS_ADOPT_DNS01") == "yes")


def acme_root(values=None):
    return Path("/etc/letsencrypt") if adopted(values) else ETC / "acme"


def cert_name(values=None):
    return values.get("FIREDASH_TLS_LINEAGE", "firedash") if values else "firedash"


def certbot_args(values=None):
    binary = values.get("FIREDASH_TLS_CERTBOT_PATH", CERTBOT) if values else CERTBOT
    require(
        Path(binary).is_file() and os.access(binary, os.X_OK),
        "Certbot missing; install the configured Certbot executable and required authenticator",
    )
    work = Path("/var/lib/letsencrypt") if adopted(values) else WORK
    logs = Path("/var/log/letsencrypt") if adopted(values) else LOGS
    return [
        binary,
        "--config",
        "/dev/null",
        "--config-dir",
        str(acme_root(values)),
        "--work-dir",
        str(work),
        "--logs-dir",
        str(logs),
        "--non-interactive",
        "--no-directory-hooks",
    ]


def lineage(values=None):
    return acme_root(values) / "live" / cert_name(values)


def check_lineage(values=None):
    """Fail closed on foreign authenticators, installers, hooks or ACME servers.

    Fresh namespaces belong to FireDash; adoption inspects the named host lineage.
    Certbot's INI has nested webroot_map syntax: inspect only renewalparams keys.
    """
    path = acme_root(values) / "renewal" / (cert_name(values) + ".conf")
    require(
        path.is_file(), "managed TLS renewal configuration missing; restore its private ACME backup"
    )
    text = path.read_text()
    params = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[renewalparams]" and "=" in line:
            key, value = line.split("=", 1)
            require(key.strip() not in params, "duplicate managed renewal parameter")
            params[key.strip()] = value.strip()
    require(
        params.get("authenticator")
        == ("dns-netcup" if values and mode(values) == "letsencrypt-dns01" else "webroot")
        and params.get("server") == SERVER,
        "managed TLS authenticator/server differs; explicit operator review required",
    )
    require(
        params.get("installer", "None") in {"None", "none", ""},
        "managed TLS must not use a Certbot installer",
    )
    expected_hook = f"/usr/bin/python3 {HELPER} hook"
    require(
        not any(params.get(k) for k in ("pre_hook", "post_hook"))
        and all(params.get(k, "") in {"", expected_hook} for k in ("renew_hook", "deploy_hook")),
        "unexpected stored ACME hooks; review before renewal",
    )
    if values and mode(values) == "letsencrypt-dns01":
        credentials = Path(params.get("dns_netcup_credentials", ""))
        protected_dns_credentials(credentials)
        if values.get("FIREDASH_TLS_DNS_CREDENTIALS"):
            require(
                str(credentials) == values["FIREDASH_TLS_DNS_CREDENTIALS"],
                "DNS credential path differs from approved configuration",
            )
    else:
        require(
            params.get("webroot_path", "").rstrip(",").strip() == str(WEBROOT),
            "managed ACME webroot differs",
        )
    if not adopted(values):
        configs = list((acme_root(values) / "renewal").glob("*.conf"))
        require(configs == [path], "unexpected additional managed ACME lineages")
    return params


def protected_dns_credentials(path):
    require(path.is_absolute(), "DNS credentials require an absolute private file path")
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == 0
        and info.st_mode & 0o777 == 0o600,
        "DNS credentials must remain regular root:root 0600 files",
    )
    for parent in path.parents:
        info = parent.lstat()
        require(
            not parent.is_symlink() and info.st_uid == 0 and info.st_mode & 0o022 == 0,
            "DNS credential parents require root ownership without group/other writes",
        )


def check_schedules(values=None):
    # Standard host Certbot jobs use /etc/letsencrypt and cannot touch our namespace.
    # Reject custom jobs referencing our directory/helper; never disable host jobs.
    if adopted(values):
        for unit in (
            "certbot.timer",
            "snap.certbot.renew.timer",
            "certbot.service",
            "snap.certbot.renew.service",
        ):
            for status in ("is-enabled", "is-active"):
                result = subprocess.run(  # nosec B603
                    ["/usr/bin/systemctl", status, unit], capture_output=True, timeout=30
                )  # noqa: S603,S607
                require(
                    result.stdout.strip()
                    not in {b"enabled", b"enabled-runtime", b"active", b"activating"},
                    "competing host Certbot scheduler; explicitly reconcile it "
                    "before DNS-01 adoption",
                )
        timers = run(
            ["systemctl", "list-unit-files", "--type=timer", "--state=enabled", "--no-legend"]
        ).decode()
        for row in timers.splitlines():
            unit = row.split()[0]
            if unit == TIMER:
                continue
            text = run(["systemctl", "cat", unit]).decode()
            service = next(
                iter(re.findall(r"^Unit=(.+)$", text, re.M)),
                unit.removesuffix(".timer") + ".service",
            )
            text += run(["systemctl", "cat", service]).decode()
            require(
                not re.search(r"certbot|letsencrypt|acme", text, re.I),
                "competing ACME timer; explicitly reconcile its lineage ownership",
            )
    candidates: list[Path] = []
    for directory in (
        Path("/etc/cron.d"),
        Path("/etc/cron.daily"),
        Path("/etc/cron.hourly"),
        Path("/var/spool/cron/crontabs"),
        Path("/etc/systemd/system"),
        Path("/run/systemd/system"),
    ):
        if directory.exists():
            candidates.extend(p for p in directory.rglob("*") if p.is_file())
    candidates.append(Path("/etc/crontab"))
    for path in candidates:
        if path.name in {"fire-tls-renew.service", TIMER}:
            continue
        if path.exists():
            text = path.read_text(errors="replace")
            if adopted(values) and ("cron" in str(path) or path == Path("/etc/crontab")):
                commands = "\n".join(
                    line
                    for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
                # Debian's packaged cron job is dormant under systemd.
                dormant = (
                    path == Path("/etc/cron.d/certbot") and "! -d /run/systemd/system" in commands
                )
                require(
                    dormant or not re.search(r"certbot|letsencrypt|acme", commands, re.I),
                    "competing ACME cron job; explicitly reconcile before adoption",
                )
            require(
                str(ETC / "acme") not in text and str(HELPER) not in text,
                "conflicting FireDash ACME scheduler/override; reconcile explicitly",
            )


def dns_addresses(host):
    records = {
        ipaddress.ip_address(row[4][0])
        for row in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
    }
    local = json.loads(run(["ip", "-j", "address", "show"]))
    local_addresses = {
        ipaddress.ip_address(a["local"])
        for interface in local
        for a in interface.get("addr_info", [])
    }
    require(
        bool(records) and all(ip.is_global and ip in local_addresses for ip in records),
        "DNS A/AAAA must all point to this server's public interface addresses; "
        "correct DNS/IPv6 before issuance",
    )
    return records


def challenge_config(host):
    return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    access_log off;
    error_log /dev/null crit;
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 503; }}
}}
"""


def nginx_apply(text):
    """Transactional FireDash site change, including rollback on reload failure."""
    previous = SITE.read_bytes() if SITE.exists() else None
    created_link = not ENABLED.exists() and not ENABLED.is_symlink()
    require(not SITE.is_symlink(), "FireDash Nginx site must be a regular file")
    require(
        created_link or ENABLED.is_symlink() and ENABLED.resolve() == SITE.resolve(),
        "FireDash enabled site is not the expected symlink",
    )
    SITE.parent.mkdir(parents=True, exist_ok=True)
    ENABLED.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic(SITE, text.encode(), 0o644)
        if created_link:
            ENABLED.symlink_to(SITE)
        run(["nginx", "-t"])
        if (
            subprocess.run(  # nosec B603
                ["/usr/bin/systemctl", "is-active", "--quiet", "nginx"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        ):  # noqa: S603,S607
            run(["systemctl", "reload", "nginx"])
        else:
            run(["systemctl", "enable", "--now", "nginx"])
    except BaseException:
        if previous is None:
            SITE.unlink(missing_ok=True)
        else:
            atomic(SITE, previous, 0o644)
        if created_link:
            ENABLED.unlink(missing_ok=True)
        raise


def atomic(path, data, permissions):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tls-")
    try:
        with os.fdopen(fd, "wb") as file:
            os.chmod(name, permissions)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def deploy(values, *, reload=True):
    host = hostname(values["FIREDASH_BASE_URL"])
    source = lineage(values)
    validate(source / "fullchain.pem", source / "privkey.pem", host)
    target = ETC / "tls"
    target.mkdir(mode=0o700, exist_ok=True)
    cert = (source / "fullchain.pem").read_bytes()
    key = (source / "privkey.pem").read_bytes()
    identity = hashlib.sha256(cert + key).hexdigest()
    generation = target / identity
    generation.mkdir(mode=0o700, exist_ok=True)
    atomic(generation / "fullchain.pem", cert, 0o644)
    atomic(generation / "privkey.pem", key, 0o600)
    validate(generation / "fullchain.pem", generation / "privkey.pem", host)
    current = target / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    require(
        not current.exists() or current.is_symlink(), "managed TLS current pointer is not a symlink"
    )
    pending = target / "next"
    pending.unlink(missing_ok=True)
    pending.symlink_to(identity)
    os.replace(pending, current)
    try:
        if reload:
            run(["nginx", "-t"])
            # Also reload on unchanged bytes: recovers an interruption after the
            # pointer swap but before Nginx acknowledged a previous reload.
            run(["systemctl", "reload", "nginx"])
            atomic(target / "deployed", identity.encode(), 0o600)
    except BaseException:
        if previous is None:
            current.unlink(missing_ok=True)
        else:
            pending.symlink_to(previous)
            os.replace(pending, current)
        raise


def certificate_identity(values):
    source = lineage(values)
    return hashlib.sha256(
        (source / "fullchain.pem").read_bytes() + (source / "privkey.pem").read_bytes()
    ).hexdigest()


def deploy_dns(values):
    validate(
        values["FIREDASH_TLS_CERT_PATH"],
        values["FIREDASH_TLS_KEY_PATH"],
        hostname(values["FIREDASH_BASE_URL"]),
    )
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])
    atomic(ETC / "tls/dns-deployed", certificate_identity(values).encode(), 0o600)


def dns_snapshot(values):
    """Retain only the original Certbot symlink targets, never rewrite renewal INI.

    Certbot retains historical archive files. Restore links after an unsuccessful
    renewal/deploy so an invalid candidate cannot become the next Nginx start.
    """
    links = {}
    source = lineage(values)
    archive = (acme_root(values) / "archive" / cert_name(values)).resolve()
    for name in ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"):
        path = source / name
        require(
            path.is_symlink() and path.resolve().is_relative_to(archive),
            "adoption requires standard Certbot live/archive symlinks",
        )
        links[name] = os.readlink(path)
    journal = ETC / "tls/dns-recovery.json"
    atomic(journal, json.dumps(links).encode(), 0o600)
    return journal


def restore_dns(values, journal):
    links = json.loads(journal.read_text())
    require(
        set(links) == {"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"},
        "invalid DNS renewal recovery journal",
    )
    source = lineage(values)
    archive = (acme_root(values) / "archive" / cert_name(values)).resolve()
    for name, target in links.items():
        require(
            (source / target).resolve().is_relative_to(archive),
            "invalid DNS renewal recovery target",
        )
        pending = source / (".firedash-" + name)
        pending.unlink(missing_ok=True)
        pending.symlink_to(target)
        os.replace(pending, source / name)


def recover_dns(values):
    journal = ETC / "tls/dns-recovery.json"
    if not journal.exists():
        return
    try:
        validate(
            values["FIREDASH_TLS_CERT_PATH"],
            values["FIREDASH_TLS_KEY_PATH"],
            hostname(values["FIREDASH_BASE_URL"]),
        )
        marker = ETC / "tls/dns-deployed"
        if not marker.exists() or marker.read_text() != certificate_identity(values):
            deploy_dns(values)
    except BaseException:
        restore_dns(values, journal)
        raise
    finally:
        journal.unlink(missing_ok=True)


def renew(values, *, use_hook=True):
    check_lineage(values)
    check_schedules(values)
    if adopted(values):
        recover_dns(values)
    if Path(values["FIREDASH_TLS_CERT_PATH"]).exists():
        try:
            validate(
                values["FIREDASH_TLS_CERT_PATH"],
                values["FIREDASH_TLS_KEY_PATH"],
                hostname(values["FIREDASH_BASE_URL"]),
                int(values.get("FIREDASH_TLS_WARNING_DAYS", "30")),
            )
        except TLSError as error:
            if "expired or not yet valid" not in str(error):
                raise
            print(
                "firedash warning: managed certificate requires validity recovery", file=sys.stderr
            )
    args = certbot_args(values) + ["renew", "--cert-name", cert_name(values)]
    if mode(values) == "letsencrypt-http01":
        args += ["--webroot-path", str(WEBROOT)]
    if use_hook:
        args += ["--deploy-hook", f"/usr/bin/python3 {HELPER} hook"]
    journal = dns_snapshot(values) if adopted(values) else None
    try:
        run(args)
        identity = certificate_identity(values)
        marker = ETC / ("tls/dns-deployed" if adopted(values) else "tls/deployed")
        if not marker.exists() or marker.read_text() != identity:
            if adopted(values):
                deploy_dns(values)
            else:
                deploy(values)
        validate(
            values["FIREDASH_TLS_CERT_PATH"],
            values["FIREDASH_TLS_KEY_PATH"],
            hostname(values["FIREDASH_BASE_URL"]),
            int(values.get("FIREDASH_TLS_WARNING_DAYS", "30")),
        )
    except BaseException:
        if journal:
            restore_dns(values, journal)
        raise
    finally:
        if journal:
            journal.unlink(missing_ok=True)


def dry_run(values):
    """Certbot's staging validation, without deploy hooks or application changes."""
    check_lineage(values)
    check_schedules(values)
    run(certbot_args(values) + ["renew", "--cert-name", cert_name(values), "--dry-run"])


def secure_directory(directory, permissions):
    directory.mkdir(parents=True, mode=permissions, exist_ok=True)
    info = directory.stat()
    require(
        not directory.is_symlink() and info.st_uid == 0 and info.st_mode & 0o777 == permissions,
        "ACME directories require root ownership and expected restrictive permissions",
    )


def prepare_dns(values):
    """DNS-01 needs outbound ACME/Netcup access only; never probe inbound ports."""
    host = hostname(values["FIREDASH_BASE_URL"])
    certbot_args(values)
    check_schedules(values)
    secure_directory(ETC / "tls", 0o700)
    if not adopted(values):
        for directory in (ETC / "acme", WORK, LOGS):
            secure_directory(directory, 0o700)
    require(
        b"dns-netcup" in run(certbot_args(values) + ["plugins", "--authenticators", "--text"]),
        "configured Certbot lacks dns-netcup; install the plugin in that Certbot environment",
    )
    if adopted(values):
        expected = lineage(values)
        require(
            values["FIREDASH_TLS_CERT_PATH"] == str(expected / "fullchain.pem")
            and values["FIREDASH_TLS_KEY_PATH"] == str(expected / "privkey.pem"),
            "DNS-01 adoption must preserve the exact existing lineage certificate paths",
        )
        check_lineage(values)
        recover_dns(values)
        # Adoption itself never invokes Certbot renewal for a healthy certificate.
        # Validate first; allow only expiry to enter normal renewal/recovery.
        try:
            remaining = validate(expected / "fullchain.pem", expected / "privkey.pem", host)
        except TLSError as error:
            if "expired or not yet valid" not in str(error):
                raise
            remaining = 0
        if remaining < int(values.get("FIREDASH_TLS_WARNING_DAYS", "30")) * 86400:
            renew(values, use_hook=False)
        else:
            atomic(ETC / "tls/dns-deployed", certificate_identity(values).encode(), 0o600)
        validate(expected / "fullchain.pem", expected / "privkey.pem", host)
        print(
            "firedash: DNS-Netcup lineage adopted; certificate paths and authentication preserved"
        )
        return
    require(
        tuple(map(str, paths()))
        == (values["FIREDASH_TLS_CERT_PATH"], values["FIREDASH_TLS_KEY_PATH"]),
        "fresh managed DNS-01 requires FireDash verified certificate paths",
    )
    protected_dns_credentials(Path(values.get("FIREDASH_TLS_DNS_CREDENTIALS", "")))
    for directory in (ETC / "acme", WORK, LOGS):
        secure_directory(directory, 0o700)
    renewal_config = acme_root(values) / "renewal" / (cert_name(values) + ".conf")
    if renewal_config.exists():
        renew(values, use_hook=False)
    else:
        require(
            not lineage(values).exists(),
            "incomplete DNS-01 lineage; inspect private ACME state before retrying",
        )
        run(
            certbot_args(values)
            + [
                "certonly",
                "--authenticator",
                "dns-netcup",
                "--dns-netcup-credentials",
                values["FIREDASH_TLS_DNS_CREDENTIALS"],
                "--dns-netcup-propagation-seconds",
                "900",
                "--cert-name",
                cert_name(values),
                "-d",
                host,
                "--email",
                values["FIREDASH_ACME_EMAIL"],
                "--agree-tos",
                "--server",
                SERVER,
            ]
        )
        check_lineage(values)
        deploy(values, reload=False)
    validate(*paths(), host, int(values.get("FIREDASH_TLS_WARNING_DAYS", "30")))


def prepare(values):
    host = hostname(values["FIREDASH_BASE_URL"])
    warning = int(values.get("FIREDASH_TLS_WARNING_DAYS", "30"))
    if mode(values) == "manual":
        validate(values["FIREDASH_TLS_CERT_PATH"], values["FIREDASH_TLS_KEY_PATH"], host, warning)
        print("firedash: TLS renewal is externally managed (manual mode)")
        return
    if mode(values) == "letsencrypt-dns01":
        prepare_dns(values)
        return
    require(cert_name(values) == "firedash", "fresh HTTP-01 uses the isolated firedash lineage")
    require(
        tuple(map(str, paths()))
        == (values["FIREDASH_TLS_CERT_PATH"], values["FIREDASH_TLS_KEY_PATH"]),
        "managed certificate paths must use the FireDash verified copy",
    )
    certbot_args(values)
    check_schedules(values)
    for directory in (ETC / "acme", WORK, LOGS):
        secure_directory(directory, 0o700)
    secure_directory(WEBROOT, 0o755)
    if (ETC / "acme/renewal/firedash.conf").exists():
        check_lineage(values)
        # Certbot decides when due, including expired certificates. No force-renewal.
        renew(values, use_hook=False)
    else:
        require(
            not lineage().exists(),
            "incomplete ACME lineage; inspect private ACME state before retrying",
        )
        require(
            not SITE.exists() or SITE.read_text() == challenge_config(host),
            "first managed issuance will not replace an existing site; "
            "external-to-managed conversion requires a separately reviewed plan",
        )
        addresses = dns_addresses(host)
        nginx_apply(challenge_config(host))
        challenge = WEBROOT / ".well-known/acme-challenge"
        challenge.mkdir(parents=True, exist_ok=True)
        probe = challenge / ("firedash-" + os.urandom(16).hex())
        probe.write_text("firedash-acme-probe")
        try:
            print(
                "firedash: Public TCP 80 must reach this server for HTTP-01; "
                "local probes cannot prove Internet reachability"
            )
            for address in addresses:
                address_text = f"[{address}]" if address.version == 6 else str(address)
                body = run(
                    [
                        "curl",
                        "--noproxy",
                        "*",
                        "--fail",
                        "--silent",
                        "--max-time",
                        "15",
                        "--resolve",
                        f"{host}:80:{address_text}",
                        f"http://{host}/.well-known/acme-challenge/{probe.name}",
                    ]
                )
                require(
                    body == b"firedash-acme-probe",
                    "HTTP-01 challenge unreachable; check port 80, firewall, DNS and IPv6",
                )
            run(
                certbot_args(values)
                + [
                    "certonly",
                    "--webroot",
                    "-w",
                    str(WEBROOT),
                    "--cert-name",
                    "firedash",
                    "-d",
                    host,
                    "--email",
                    values["FIREDASH_ACME_EMAIL"],
                    "--agree-tos",
                    "--server",
                    SERVER,
                ]
            )
            check_lineage(values)
            deploy(values, reload=False)
        finally:
            probe.unlink(missing_ok=True)
    validate(*paths(), host, warning)


def verify_owned_file(path, expected):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == 0
        and info.st_mode & 0o022 == 0,
        "TLS hook/unit must be a regular protected root-owned file",
    )
    require(path.read_bytes() == expected, "installed TLS hook/unit differs from release")


def verify(values):
    host = hostname(values["FIREDASH_BASE_URL"])
    validate(
        values["FIREDASH_TLS_CERT_PATH"],
        values["FIREDASH_TLS_KEY_PATH"],
        host,
        int(values.get("FIREDASH_TLS_WARNING_DAYS", "30")),
    )
    text = SITE.read_text()
    for directive, key in (
        ("ssl_certificate", "FIREDASH_TLS_CERT_PATH"),
        ("ssl_certificate_key", "FIREDASH_TLS_KEY_PATH"),
    ):
        require(
            re.findall(r"^\s*" + directive + r"\s+([^;]+);", text, re.M) == [values[key]],
            "Nginx certificate paths differ from persisted configuration",
        )
    run(["nginx", "-t"])
    if mode(values) == "manual":
        print("firedash: TLS renewal is externally managed; automatic renewal is not verified")
        return
    certbot_args(values)
    check_lineage(values)
    check_schedules(values)
    require(
        ("dns-netcup" if mode(values) == "letsencrypt-dns01" else "webroot").encode()
        in run(certbot_args(values) + ["plugins", "--authenticators", "--text"]),
        "configured Certbot authenticator unavailable",
    )
    for filename in ("tls.py", "configuration.py", "legacy-schema-1.json"):
        path = HELPER.parent / filename
        verify_owned_file(path, Path(__file__).with_name(filename).read_bytes())
    for unit in ("fire-tls-renew.service", TIMER):
        expected = Path(__file__).parent.parent / "systemd" / unit
        verify_owned_file(UNITS / unit, expected.read_bytes())
        effective = run(["systemctl", "cat", unit]).decode()
        require(".d/" not in effective, "TLS renewal unit has unreviewed overrides")
    run(["systemctl", "is-enabled", "--quiet", TIMER])
    run(["systemctl", "is-active", "--quiet", TIMER])
    print("firedash: managed TLS authenticator and renewal timer verified")


@contextlib.contextmanager
def renewal_lock():
    import fcntl

    with LOCK.open("w") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    require(os.geteuid() == 0, "TLS lifecycle requires root")
    command = sys.argv[1]
    if command == "prepare":
        # Caller validates protected configuration before passing literal CLI values.
        values = {k: os.environ[k] for k in config.INSTALL_KEYS if k in os.environ}
        config.validate_install(values)
        prepare(values)
    else:
        values = settings()
        if command == "verify":
            verify(values)
        elif command == "nginx":
            host = hostname(values["FIREDASH_BASE_URL"])
            validate(values["FIREDASH_TLS_CERT_PATH"], values["FIREDASH_TLS_KEY_PATH"], host)
            template = Path(__file__).parent.parent / "nginx/fire-backend.conf"
            text = (
                template.read_text()
                .replace("__SERVER_NAME__", host)
                .replace("__TLS_CERT_PATH__", values["FIREDASH_TLS_CERT_PATH"])
                .replace("__TLS_KEY_PATH__", values["FIREDASH_TLS_KEY_PATH"])
            )
            nginx_apply(text)
            if mode(values) != "manual":
                loaded_identity = hashlib.sha256(
                    Path(values["FIREDASH_TLS_CERT_PATH"]).read_bytes()
                    + Path(values["FIREDASH_TLS_KEY_PATH"]).read_bytes()
                ).hexdigest()
                marker = ETC / ("tls/dns-deployed" if adopted(values) else "tls/deployed")
                atomic(marker, loaded_identity.encode(), 0o600)
        else:
            require(mode(values) != "manual", "renewal is restricted to FireDash managed mode")
            if command in {"renew", "dry-run"}:
                with renewal_lock():
                    if command == "dry-run":
                        dry_run(values)
                    else:
                        renew(values)
            elif command == "hook":
                require(
                    os.environ.get("RENEWED_LINEAGE") == str(lineage(values)),
                    "unexpected ACME deploy-hook lineage",
                )
                check_lineage(values)
                if adopted(values):
                    deploy_dns(values)
                else:
                    deploy(values)
            else:
                raise TLSError("unknown TLS lifecycle command")


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise InterruptedError("TLS lifecycle interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (
        TLSError,
        config.ConfigurationError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ):
        print(
            "firedash error: "
            + (
                str(sys.exc_info()[1])
                if isinstance(sys.exc_info()[1], TLSError | config.ConfigurationError)
                else "TLS lifecycle failed; inspect DNS, port 80, protected ACME logs "
                "and renewal configuration"
            ),
            file=sys.stderr,
        )
        sys.exit(1)
