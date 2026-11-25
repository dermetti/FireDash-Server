"""Root-only TLS lifecycle. No Django imports or database access.

The installer already holds firedash-install.lock; the scheduled entrypoint takes
the same lock. Fresh issuance uses a separate Certbot namespace and verified
copies. Explicit DNS lineage adoption retains the original live/archive lineage.
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
import dns_auth

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
        # Do not let ambient DNS credentials, Python/loader overrides or shell
        # startup settings become a second privileged plugin configuration path.
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        if os.name == "posix"
        else None,
    ) as process:
        try:
            output, diagnostic = process.communicate(
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
    category = "TLS command failed; inspect protected ACME logs / Nginx configuration"
    if process.returncode and "certbot" in Path(args[0]).name:
        # Classify only; plugin diagnostics can include secrets and are never relayed.
        evidence = (output + diagnostic).lower()
        if any(
            word in evidence
            for word in (
                b"invalid api",
                b"authentication failed",
                b"invalid credentials",
                b"api request forbidden",
                b"provider authentication",
            )
        ):
            category = "DNS provider authentication failed; inspect protected provider credentials"
        elif any(
            word in evidence
            for word in (b"propagation", b"incorrect txt", b"nxdomain", b"dns problem")
        ):
            category = (
                "DNS propagation/validation failed; inspect authoritative DNS and propagation wait"
            )
        else:
            category = "ACME issuance/renewal failed; inspect protected ACME logs and retry safely"
    require(process.returncode == 0, category)
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


def protected_acme_file(path):
    """Read/execute only root-controlled ACME configuration and programs."""
    for candidate in {path, path.resolve(strict=True)}:
        info = candidate.stat()
        require(
            stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0,
            "ACME executable/configuration must be a protected root-owned regular file",
        )
        for parent in candidate.parents:
            info = parent.stat()
            require(
                info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "ACME executable/configuration parents must be root-controlled",
            )


def certbot_args(values=None):
    binary = values.get("FIREDASH_TLS_CERTBOT_PATH", CERTBOT) if values else CERTBOT
    require(
        Path(binary).is_file() and os.access(binary, os.X_OK),
        "Certbot missing; install the configured Certbot executable and required authenticator",
    )
    require(Path(binary).name in {"certbot", "certbot.exe"}, "expected approved Certbot executable")
    protected_acme_file(Path(binary))
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
    protected_acme_file(path)
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
    expected_auth = (
        dns_auth.authenticator(values)
        if values and mode(values) == "letsencrypt-dns01"
        else "webroot"
    )
    require(
        params.get("authenticator") == expected_auth and params.get("server") == SERVER,
        "managed TLS authenticator/server differs; explicit operator review required",
    )
    require(
        params.get("installer", "None") in {"None", "none", ""},
        "managed TLS must not use a Certbot installer",
    )
    expected_hook = f"/usr/bin/python3 {HELPER} hook"
    require(
        not any(
            v
            for k, v in params.items()
            if ("hook" in k and k not in {"renew_hook", "deploy_hook"}) or k.endswith("_command")
        )
        and all(params.get(k, "") in {"", expected_hook} for k in ("renew_hook", "deploy_hook")),
        "unexpected stored ACME hooks; review before renewal",
    )
    if values and mode(values) == "letsencrypt-dns01":
        dns_auth.validate(values, require)
        dns_auth.credentials(values, params, protected_dns_credentials, require)
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
    listen 80 default_server;
    listen [::]:80 default_server;
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


def packaged_default_site():
    """Return only the enabled, unmodified nginx-common conffile symlink."""
    enabled = ENABLED.parent / "default"
    if not enabled.exists() and not enabled.is_symlink():
        return None
    available = SITE.parent / "default"
    message = (
        "Nginx sites-enabled/default is not the unmodified nginx-common default; "
        "review and disable conflicting administrator configuration manually"
    )
    require(enabled.is_symlink() and enabled.resolve() == available, message)
    require(available.is_file() and not available.is_symlink(), message)
    metadata = run(
        ["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Status}\n${Conffiles}", "nginx-common"]
    ).decode()
    require(metadata.startswith("installed\n"), message)
    matches = re.findall(r"^ " + re.escape(str(available)) + r" ([0-9a-f]{32})$", metadata, re.M)
    require(len(matches) == 1, message)
    # dpkg's conffile identity uses MD5; this is not a cryptographic trust decision.
    require(
        hashlib.md5(available.read_bytes(), usedforsecurity=False).hexdigest() == matches[0],
        message,
    )
    return enabled


def nginx_ownership_preflight():
    default = packaged_default_site()
    if ENABLED.parent.exists():
        for path in ENABLED.parent.iterdir():
            if path in (ENABLED, default):
                continue
            # Detect ordinary Debian enabled-vhost conflicts before maintenance.
            # nginx -t remains authoritative for includes and full syntax.
            text = re.sub(r"#[^\n]*", "", path.read_text())
            listeners = re.findall(r"\blisten\s+([^;]+);", text)
            require(
                not any(
                    "default_server" in listener.split()
                    and listener.split()[0]
                    in {
                        f"{address}{port}"
                        for address in ("", "*:", "0.0.0.0:", "[::]:")
                        for port in (80, 443)
                    }
                    for listener in listeners
                ),
                "custom Nginx default_server conflicts with FireDash; "
                "review sites-enabled virtual hosts manually (no custom files changed)",
            )
    return default


def nginx_apply(text):
    """Transactional FireDash site change, including rollback on reload failure."""
    default = nginx_ownership_preflight()
    default_target = os.readlink(default) if default is not None else None
    previous = SITE.read_bytes() if SITE.exists() else None
    created_link = not ENABLED.exists() and not ENABLED.is_symlink()
    require(not SITE.is_symlink(), "FireDash Nginx site must be a regular file")
    require(
        created_link or ENABLED.is_symlink() and ENABLED.resolve() == SITE.resolve(),
        "FireDash enabled site is not the expected symlink",
    )
    SITE.parent.mkdir(parents=True, exist_ok=True)
    ENABLED.parent.mkdir(parents=True, exist_ok=True)
    activation_attempted = False
    active = False
    try:
        if default is not None:
            default.unlink()
        atomic(SITE, text.encode(), 0o644)
        if created_link:
            ENABLED.symlink_to(SITE)
        run(["nginx", "-t"])
        active = (
            subprocess.run(  # nosec B603
                ["/usr/bin/systemctl", "is-active", "--quiet", "nginx"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )  # noqa: S603,S607
        activation_attempted = True
        if active:
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
        if default is not None and default_target is not None:
            default.symlink_to(default_target)
        if activation_attempted and active:
            # A failed reload may have partially applied; reload the restored
            # configuration too. Keep the original failure if recovery fails.
            with contextlib.suppress(TLSError, OSError, subprocess.SubprocessError):
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
        raise


def sync_directory(path):
    """Persist directory entries as well as file contents on the supported OS."""
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic(path, data, permissions, *, exclusive=False):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tls-")
    try:
        with os.fdopen(fd, "wb") as file:
            os.chmod(name, permissions)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if exclusive:
            # Publish a complete journal without ever replacing unresolved state.
            os.link(name, path)
        else:
            os.replace(name, path)
        sync_directory(path.parent)
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


def deploy_dns(values, *, recovering=False):
    validate(
        values["FIREDASH_TLS_CERT_PATH"],
        values["FIREDASH_TLS_KEY_PATH"],
        hostname(values["FIREDASH_BASE_URL"]),
    )
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])
    if recovering:
        # The first wait can join an in-flight reload from an interrupted hook.
        # Once it completes, request a distinct reload of the restored files.
        run(["systemctl", "reload", "nginx"])
    atomic(ETC / "tls/dns-deployed", certificate_identity(values).encode(), 0o600)


def dns_snapshot(values):
    """Retain only the original Certbot symlink targets, never rewrite renewal INI.

    Certbot retains historical archive files. Restore links after an unsuccessful
    renewal/deploy so an invalid candidate cannot become the next Nginx start.
    """
    journal = ETC / "tls/dns-recovery.json"
    require(
        not journal.exists() and not journal.is_symlink(),
        "unresolved DNS recovery journal; run recovery before renewing",
    )
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
    record = {
        "version": 2,
        "state": "pending",
        "lineage": str(source),
        "links": links,
        "identity": certificate_identity(values),
    }
    # Validate the entire snapshot before it can authorize any later writes.
    dns_recovery_record(values, record)
    atomic(journal, json.dumps(record).encode(), 0o600, exclusive=True)
    return journal


def dns_recovery_record(values, record):
    """Validate all old archive targets before touching even the first live link."""
    source = lineage(values)
    require(
        isinstance(record, dict)
        and set(record) == {"version", "state", "lineage", "links", "identity"}
        and record["version"] == 2
        and record["state"] in {"pending", "rollback"}
        and record["lineage"] == str(source),
        "invalid DNS recovery journal; retain it for private operator recovery",
    )
    links = record["links"]
    require(
        isinstance(links, dict)
        and set(links) == {"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"},
        "invalid DNS renewal recovery journal",
    )
    archive = (acme_root(values) / "archive" / cert_name(values)).resolve()
    require(
        source.resolve() == source.absolute()
        and archive == (acme_root(values) / "archive" / cert_name(values)).absolute(),
        "DNS recovery requires standard live/archive directories",
    )
    versions = set()
    for name, target in links.items():
        require(isinstance(target, str), "invalid DNS recovery target")
        match = re.fullmatch(
            r"\.\./\.\./archive/"
            + re.escape(cert_name(values))
            + "/"
            + name[:-4]
            + r"([1-9][0-9]*)\.pem",
            target,
        )
        require(match is not None, "invalid DNS recovery target")
        versions.add(match[1] if match is not None else "")
        path = source / target
        require(
            not path.is_symlink() and path.is_file() and path.resolve().parent == archive,
            "DNS recovery archive missing or unsafe; preserve journal and restore private backup",
        )
        protected_acme_file(path)
        if name == "privkey.pem":
            require(
                path.stat().st_mode & 0o777 == 0o600, "DNS recovery private key requires mode 0600"
            )
    require(len(versions) == 1, "inconsistent DNS recovery archive generation")
    identity = hashlib.sha256(
        (source / links["fullchain.pem"]).read_bytes()
        + (source / links["privkey.pem"]).read_bytes()
    ).hexdigest()
    require(record["identity"] == identity, "DNS recovery archive identity mismatch")
    try:
        validate(
            source / links["fullchain.pem"],
            source / links["privkey.pem"],
            hostname(values["FIREDASH_BASE_URL"]),
        )
    except TLSError as error:
        # An expired generation may be snapshotted for a recovery renewal, but
        # deploy_dns still refuses to activate it if that renewal fails.
        if "expired or not yet valid" not in str(error):
            raise
    return links


def read_dns_journal(values, journal):
    protected_acme_file(journal)
    require(
        not journal.is_symlink()
        and journal.stat().st_mode & 0o777 == 0o600
        and journal.stat().st_size <= 8192,
        "unsafe DNS recovery journal",
    )
    record = json.loads(journal.read_text())
    return record, dns_recovery_record(values, record)


def restore_dns(values, journal):
    record, links = read_dns_journal(values, journal)
    source = lineage(values)
    for name, target in links.items():
        require((source / name).is_symlink(), "DNS recovery requires live symlinks; retain journal")
        pending = source / (".firedash-" + name)
        require(
            not pending.exists()
            and not pending.is_symlink()
            or pending.is_symlink()
            and os.readlink(pending) == target,
            "unsafe DNS recovery temporary path; retain journal",
        )
    if record["state"] != "rollback":
        record["state"] = "rollback"
        atomic(journal, json.dumps(record).encode(), 0o600)
    for name, target in links.items():
        pending = source / (".firedash-" + name)
        pending.unlink(missing_ok=True)
        pending.symlink_to(target)
        os.replace(pending, source / name)
        sync_directory(source)
    require(
        all(
            (source / name).is_symlink() and os.readlink(source / name) == target
            for name, target in links.items()
        )
        and certificate_identity(values) == record["identity"],
        "DNS recovery incomplete; retain journal",
    )
    # A reload may have succeeded before an interruption. Always restore Nginx
    # too; a marker alone cannot establish which generation workers loaded.
    deploy_dns(values, recovering=True)


def recover_dns(values):
    journal = ETC / "tls/dns-recovery.json"
    if not journal.exists() and not journal.is_symlink():
        return
    try:
        restore_dns(values, journal)
        complete_dns(values, journal)
    except BaseException as error:
        raise TLSError(
            "DNS recovery incomplete; journal retained. Do not renew or edit renewal metadata; "
            "restore missing archives from encrypted backup and retry tls.py recover"
        ) from error


def complete_dns(values, journal):
    source = lineage(values)
    dns_recovery_record(
        values,
        {
            "version": 2,
            "state": "pending",
            "lineage": str(source),
            "links": {
                name: os.readlink(source / name)
                for name in ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem")
            },
            "identity": certificate_identity(values),
        },
    )
    validate(
        values["FIREDASH_TLS_CERT_PATH"],
        values["FIREDASH_TLS_KEY_PATH"],
        hostname(values["FIREDASH_BASE_URL"]),
    )
    marker = ETC / "tls/dns-deployed"
    require(
        marker.is_file() and marker.read_text() == certificate_identity(values),
        "DNS activation incomplete; recovery journal retained",
    )
    journal.unlink()
    sync_directory(journal.parent)


def renew(values, *, use_hook=True):
    check_lineage(values)
    if mode(values) == "letsencrypt-dns01":
        dns_auth.check_plugin(
            values, certbot_args(values), run, require, protect=protected_acme_file
        )
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
            recover_dns(values)
        raise
    if journal:
        complete_dns(values, journal)


def dry_run(values):
    """Certbot's staging validation, without deploy hooks or application changes."""
    check_lineage(values)
    if adopted(values):
        recover_dns(values)
    if mode(values) == "letsencrypt-dns01":
        dns_auth.check_plugin(
            values, certbot_args(values), run, require, protect=protected_acme_file
        )
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
    # Missing backend is compatible only with an already recorded Installer B
    # Netcup lineage; it must never authorize fresh legacy issuance.
    if dns_auth.backend(values) == "legacy-netcup":
        require(
            (acme_root(values) / "renewal" / (cert_name(values) + ".conf")).is_file(),
            "new DNS installations require an explicit DNS backend and plugin approval",
        )
    dns_auth.check_plugin(values, certbot_args(values), run, require, protect=protected_acme_file)
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
        print("firedash: DNS lineage adopted; certificate paths and authentication preserved")
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
                *dns_auth.issuance_args(values, protected_dns_credentials, require),
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
    nginx_ownership_preflight()
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
    if mode(values) == "letsencrypt-dns01":
        dns_auth.check_plugin(
            values, certbot_args(values), run, require, protect=protected_acme_file
        )
    else:
        require(
            b"webroot" in run(certbot_args(values) + ["plugins", "--authenticators", "--text"]),
            "configured Certbot authenticator unavailable",
        )
    for filename in (
        "tls.py",
        "dns_auth.py",
        "tls_backup.py",
        "configuration.py",
        "legacy-schema-1.json",
    ):
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
            overrides = config.read(ETC / "overrides.env")
            config.validate_overrides(overrides)
            # Keep the established default ceiling and multipart headroom when
            # the supported aggregate upload override exceeds its default.
            upload_bytes = int(overrides.get("MAX_INGEST_UPLOAD_BYTES", 268435456))
            outer_mib = max(300, (upload_bytes * 300 + 268435456 - 1) // 268435456)
            text = text.replace("client_max_body_size 300m;", f"client_max_body_size {outer_mib}m;")
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
            if command in {"renew", "dry-run", "recover"}:
                with renewal_lock():
                    if command == "recover":
                        require(adopted(values), "recovery applies to adopted DNS lineages")
                        recover_dns(values)
                    elif command == "dry-run":
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
