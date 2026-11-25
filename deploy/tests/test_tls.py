"""Disposable Installer B tests. No network, ACME account, host files or database.

Real OpenSSL checks use synthetic keys only. Native symlink/Nginx/systemd checks
are explicit Linux gates; mocked issuance is never evidence of ACME integration.
"""

import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/lib"))
spec = importlib.util.spec_from_file_location("installer_tls", ROOT / "deploy/lib/tls.py")
assert spec and spec.loader
tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tls)
CHECK_SCHEDULES = tls.check_schedules
PROTECT_DNS_CREDENTIALS = tls.protected_dns_credentials
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="native Linux symlinks/permissions")


@pytest.fixture
def backup_module(monkeypatch):
    import tls_backup

    monkeypatch.setattr(tls_backup, "tls", tls)
    return tls_backup


@pytest.fixture
def tree(tmp_path, monkeypatch):
    for name, value in {
        "ETC": tmp_path / "etc",
        "WEBROOT": tmp_path / "webroot",
        "WORK": tmp_path / "work",
        "LOGS": tmp_path / "logs",
        "SITE": tmp_path / "nginx/available/fire-backend",
        "ENABLED": tmp_path / "nginx/enabled/fire-backend",
        "HELPER": tmp_path / "helpers/tls.py",
        "UNITS": tmp_path / "units",
        "LOCK": tmp_path / "lock",
    }.items():
        monkeypatch.setattr(tls, name, value)
    tls.ETC.mkdir()
    monkeypatch.setattr(tls, "protected_acme_file", lambda path: None)
    monkeypatch.setattr(tls, "SERVER", "https://acme-staging-v02.api.letsencrypt.org/directory")
    executable = tmp_path / "certbot"
    executable.touch(mode=0o755)
    monkeypatch.setattr(tls, "CERTBOT", str(executable))
    monkeypatch.setattr(tls, "check_schedules", lambda *a: None)
    return {
        "FIREDASH_INSTALL_SCHEMA_VERSION": "2",
        "FIREDASH_BASE_URL": "https://firedash.example.org",
        "FIREDASH_TLS_MODE": "letsencrypt-http01",
        "FIREDASH_TLS_CERT_PATH": tls.paths()[0].as_posix(),
        "FIREDASH_TLS_KEY_PATH": tls.paths()[1].as_posix(),
        "FIREDASH_ACME_EMAIL": "acme@example.org",
        "FIREDASH_ACME_AGREE_TOS": "yes",
        "FIREDASH_REQUESTED_REF": "main",
    }


@pytest.fixture
def openssl(monkeypatch):
    executable = shutil.which("openssl")
    if os.name == "nt":
        executable = "C:/Program Files/Git/mingw64/bin/openssl.exe"
    assert executable and Path(executable).is_file(), "OpenSSL required for certificate tests"
    original = tls.run

    def run(args, **kwargs):
        assert args[0] == "openssl", "certificate fixture must not invoke host/network commands"
        return original([executable, *args[1:]], **kwargs)

    monkeypatch.setattr(tls, "run", run)


@pytest.fixture
def certificate(tmp_path):
    def make(name="valid", *, days=90, host="firedash.example.org"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic test CA")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=10))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        directory = tmp_path / name
        directory.mkdir()
        cert_path, key_path = directory / "fullchain.pem", directory / "privkey.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path

    return make


def put_lineage(pair, *, authenticator="webroot", extra=""):
    tls.lineage().mkdir(parents=True, exist_ok=True)
    for source in pair:
        shutil.copyfile(source, tls.lineage() / source.name)
    renewal = tls.ETC / "acme/renewal"
    renewal.mkdir(parents=True, exist_ok=True)
    (renewal / "firedash.conf").write_text(
        f"[renewalparams]\nauthenticator = {authenticator}\nserver = {tls.SERVER}\n"
        f"webroot_path = {tls.WEBROOT},\n{extra}"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://valid.example.org",
        "https://-bad.example.org",
        "https://bad..org",
        "https://good.example.org:443",
        "https://127.0.0.1",
        "https://host.example.org/path",
        "https://host.example.org;bad",
        "https://host.example.org#x",
    ],
)
def test_hostname_rejected(origin):
    with pytest.raises(tls.TLSError):
        tls.hostname(origin)


def test_hostname_and_modes(tree):
    assert tls.hostname(tree["FIREDASH_BASE_URL"]) == "firedash.example.org"
    tls.config.validate_install(tree)
    for key, value in [
        ("FIREDASH_TLS_MODE", "dns"),
        ("FIREDASH_ACME_AGREE_TOS", "no"),
        ("FIREDASH_ACME_EMAIL", "bad"),
        ("FIREDASH_TLS_WARNING_DAYS", "0"),
        ("FIREDASH_TLS_WARNING_DAYS", "61"),
        ("FIREDASH_TLS_KEY_PATH", "/tmp/key;include"),
    ]:
        with pytest.raises(tls.config.ConfigurationError):
            tls.config.validate_install(dict(tree, **{key: value}))


def test_pair_hostname_expiration_and_warning(tree, certificate, openssl, capsys):
    cert, key = certificate()
    assert tls.validate(cert, key, "firedash.example.org") > 80 * 86400
    _, other = certificate("other")
    with pytest.raises(tls.TLSError, match="mismatch"):
        tls.validate(cert, other, "firedash.example.org")
    with pytest.raises(tls.TLSError):
        tls.validate(cert, key, "wrong.example.org")
    with pytest.raises(tls.TLSError, match="expired"):
        tls.validate(*certificate("expired", days=-1), "firedash.example.org")
    tls.validate(*certificate("near", days=20), "firedash.example.org", warning_days=30)
    assert "warning window" in capsys.readouterr().err


@pytest.mark.parametrize(
    "dns,local,success",
    [
        (["8.8.8.8"], ["8.8.8.8"], True),
        (["8.8.8.8", "2001:4860:4860::8888"], ["8.8.8.8"], False),
        (["9.9.9.9"], ["8.8.8.8"], False),
        (["127.0.0.1"], ["127.0.0.1"], False),
        ([], [], False),
    ],
)
def test_dns_all_a_aaaa_must_match_interfaces(monkeypatch, dns, local, success):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (ip, 80)) for ip in dns]
    )
    monkeypatch.setattr(
        tls,
        "run",
        lambda args: json.dumps([{"addr_info": [{"local": ip} for ip in local]}]).encode(),
    )
    if success:
        assert tls.dns_addresses("firedash.example.org") == {ipaddress.ip_address(ip) for ip in dns}
    else:
        with pytest.raises(tls.TLSError, match="DNS"):
            tls.dns_addresses("firedash.example.org")


@pytest.mark.parametrize(
    "auth,extra",
    [
        ("dns-netcup", "dns_netcup_credentials = /private/unchanged\n"),
        ("webroot", "deploy_hook = hostile\n"),
        ("webroot", "installer = nginx\n"),
        ("webroot", "server = https://other.invalid\n"),
    ],
)
def test_foreign_or_modified_lineage_is_never_adopted(tree, certificate, auth, extra):
    put_lineage(certificate(), authenticator=auth, extra=extra)
    before = {p: p.read_bytes() for p in (tls.ETC / "acme").rglob("*") if p.is_file()}
    with pytest.raises(tls.TLSError):
        tls.check_lineage()
    assert all(path.read_bytes() == contents for path, contents in before.items())


def test_missing_certbot_and_renewal_config_fail(tree, monkeypatch):
    monkeypatch.setattr(tls, "CERTBOT", str(tls.ETC / "missing"))
    with pytest.raises(tls.TLSError, match="Certbot missing"):
        tls.certbot_args()
    with pytest.raises(tls.TLSError, match="configuration missing"):
        tls.check_lineage()


def test_manual_and_external_dns01_are_read_only(tree, certificate, openssl, capsys):
    pair = certificate()
    external = tls.ETC / "netcup-renewal.conf"
    external.write_text("authenticator = dns-netcup\ndns_netcup_credentials = /root/private\n")
    before = external.read_bytes()
    tree.update(
        FIREDASH_TLS_MODE="manual",
        FIREDASH_TLS_CERT_PATH=str(pair[0]),
        FIREDASH_TLS_KEY_PATH=str(pair[1]),
    )
    tls.prepare(tree)
    assert external.read_bytes() == before
    assert not (tls.ETC / "acme").exists()
    assert "externally managed" in capsys.readouterr().out


@LINUX
@pytest.mark.parametrize("failure", ["dns", "port", "acme", "interrupt", None])
def test_first_install_bootstrap_retry_and_activation(
    tree, certificate, openssl, monkeypatch, failure
):
    pair = certificate()
    validate_run = tls.run
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        if args[0] == "openssl":
            return validate_run(args, **kwargs)
        if args[0] == "curl":
            if failure == "port":
                raise tls.TLSError("challenge unreachable")
            return b"firedash-acme-probe"
        if args[0] == tls.CERTBOT:
            assert "certonly" in args and "--webroot" in args
            if failure in {"acme", "interrupt"}:
                raise tls.TLSError("staging rate/network/interruption fixture")
            put_lineage(pair)
        return b""

    monkeypatch.setattr(tls, "run", fake)

    def bootstrap(text):
        tls.SITE.parent.mkdir(parents=True, exist_ok=True)
        tls.SITE.write_text(text)

    monkeypatch.setattr(tls, "nginx_apply", bootstrap)

    def dns(host):
        if failure == "dns":
            raise tls.TLSError("incorrect DNS")
        return {ipaddress.ip_address("8.8.8.8")}

    monkeypatch.setattr(tls, "dns_addresses", dns)
    monkeypatch.setattr(
        tls,
        "secure_directory",
        lambda path, permissions: path.mkdir(parents=True, mode=permissions, exist_ok=True),
    )
    if failure:
        with pytest.raises(tls.TLSError):
            tls.prepare(tree)
        assert not tls.paths()[0].exists()
        assert not any(c[:2] == ["systemctl", "reload"] for c in calls)
        failure = None
    tls.prepare(tree)
    assert tls.paths()[0].read_bytes() == pair[0].read_bytes()
    assert "listen 443" not in tls.SITE.read_text()
    assert "location / { return 503; }" in tls.SITE.read_text()
    assert not list((tls.WEBROOT / ".well-known/acme-challenge").iterdir())
    final = (ROOT / "deploy/nginx/fire-backend.conf").read_text()
    final = (
        final.replace("__SERVER_NAME__", "firedash.example.org")
        .replace("__TLS_CERT_PATH__", str(tls.paths()[0]))
        .replace("__TLS_KEY_PATH__", str(tls.paths()[1]))
    )
    tls.nginx_apply(final)
    assert "listen 443 ssl" in tls.SITE.read_text()
    assert "location / {\n        return 301" in tls.SITE.read_text()


@LINUX
@pytest.mark.parametrize("bad_command", [None, "nginx", "systemctl"])
def test_verified_copy_activation_rolls_back(tree, certificate, openssl, monkeypatch, bad_command):
    old = certificate("old")
    put_lineage(old)
    tls.deploy(tree, reload=False)
    previous = os.readlink(tls.ETC / "tls/current")
    new = certificate("new")
    put_lineage(new)
    validate_run = tls.run
    calls = []

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return validate_run(args, **kwargs)
        calls.append(args)
        if args[0] == bad_command:
            raise tls.TLSError("failed validation/reload")
        return b""

    monkeypatch.setattr(tls, "run", fake)
    if bad_command:
        with pytest.raises(tls.TLSError):
            tls.deploy(tree)
        assert os.readlink(tls.ETC / "tls/current") == previous
        if bad_command == "nginx":
            assert not any(c[:2] == ["systemctl", "reload"] for c in calls)
    else:
        tls.deploy(tree)
        assert tls.paths()[0].read_bytes() == new[0].read_bytes()
        assert (tls.ETC / "tls/current/privkey.pem").stat().st_mode & 0o077 == 0


def test_failed_renewal_never_deploys_or_touches_application(tree, certificate, monkeypatch):
    put_lineage(certificate())
    calls = []

    def failed(args, **kwargs):
        calls.append(args)
        raise tls.TLSError("network failure")

    monkeypatch.setattr(tls, "run", failed)
    monkeypatch.setattr(
        tls, "deploy", lambda *a, **k: pytest.fail("must not deploy failed renewal")
    )
    with pytest.raises(tls.TLSError):
        tls.renew(tree)
    assert len(calls) == 1
    assert calls[0][calls[0].index("--cert-name") + 1] == "firedash"
    assert "--no-directory-hooks" in calls[0]
    assert not any("gunicorn" in arg or "manage.py" in arg for arg in calls[0])


def test_dry_run_is_scoped_and_never_runs_deploy_hook(tree, certificate, monkeypatch):
    put_lineage(certificate())
    calls = []
    monkeypatch.setattr(tls, "run", lambda args: calls.append(args))
    monkeypatch.setattr(tls, "deploy", lambda *a, **k: pytest.fail("dry run must not deploy"))
    tls.dry_run(tree)
    assert len(calls) == 1 and "--dry-run" in calls[0]
    assert "--cert-name" in calls[0] and "firedash" in calls[0]
    assert "--deploy-hook" not in calls[0] and "--run-deploy-hooks" not in calls[0]


@pytest.mark.parametrize("upload_override", [None, "536870912"])
def test_final_https_render_and_activation_marker(
    tree, certificate, openssl, monkeypatch, upload_override
):
    pair = certificate()
    tls.paths()[0].parent.mkdir(parents=True)
    for source, destination in zip(pair, tls.paths(), strict=True):
        shutil.copyfile(source, destination)
    monkeypatch.setattr(tls.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(tls.sys, "argv", ["tls.py", "nginx"])
    monkeypatch.setattr(tls, "settings", lambda: tree)
    rendered: list[str] = []
    monkeypatch.setattr(tls, "nginx_apply", rendered.append)
    if upload_override:
        (tls.ETC / "overrides.env").write_text(f"MAX_INGEST_UPLOAD_BYTES={upload_override}\n")
    tls.main()
    expected = (ROOT / "deploy/nginx/fire-backend.conf").read_text()
    expected = (
        expected.replace("__SERVER_NAME__", "firedash.example.org")
        .replace("__TLS_CERT_PATH__", tree["FIREDASH_TLS_CERT_PATH"])
        .replace("__TLS_KEY_PATH__", tree["FIREDASH_TLS_KEY_PATH"])
    )
    if upload_override:
        expected = expected.replace("client_max_body_size 300m;", "client_max_body_size 600m;")
    assert rendered == [expected]
    assert (tls.ETC / "tls/deployed").read_text() == hashlib.sha256(
        pair[0].read_bytes() + pair[1].read_bytes()
    ).hexdigest()


@LINUX
def test_timer_and_installer_share_exclusive_lock(tree):
    with tls.renewal_lock():
        with pytest.raises(BlockingIOError), tls.renewal_lock():
            pytest.fail("concurrent renewal entered installer critical section")
    with tls.renewal_lock():
        pass


def test_command_errors_suppress_child_output(monkeypatch, capsys):
    class Process:
        returncode = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, **kwargs):
            return b"private-value", b"private-value"

    monkeypatch.setattr(tls.subprocess, "Popen", lambda *a, **k: Process())
    with pytest.raises(tls.TLSError) as error:
        tls.run(["fixture"])
    assert "private-value" not in str(error.value) + capsys.readouterr().out


def test_timed_out_certbot_kills_hook_session_before_unlock(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(tls.signal, "SIGKILL", 9, raising=False)

    class Process:
        pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, **kwargs):
            raise subprocess.TimeoutExpired("fixture-certbot", 2400)

        def wait(self):
            calls.append("wait")

    def spawn(args, **kwargs):
        assert kwargs["start_new_session"] is True
        assert set(kwargs["env"]) == {"PATH", "HOME", "LANG", "LC_ALL"}
        assert "PYTHONPATH" not in kwargs["env"]
        assert "NETCUP_API_PASSWORD" not in kwargs["env"]
        return Process()

    monkeypatch.setattr(tls.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        tls,
        "os",
        SimpleNamespace(name="posix", killpg=lambda pid, signal: calls.append((pid, signal))),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        tls.run(["fixture-certbot"])
    assert calls == [(12345, tls.signal.SIGKILL), "wait"]


def test_unit_is_narrow_and_does_not_load_runtime_secrets():
    text = (ROOT / "deploy/systemd/fire-tls-renew.service").read_text()
    assert "ProtectSystem=strict" in text and "UMask=0077" in text
    assert "EnvironmentFile" not in text and "LoadCredential" not in text
    assert "manage.py" not in text and "fire-backend.service" not in text
    assert "RandomizedDelaySec=1h" in (ROOT / "deploy/systemd/fire-tls-renew.timer").read_text()
    # Backup errors must not propagate through a required/ordered dependency.
    assert "Requires=" not in text and "BindsTo=" not in text
    assert "ExecStartPost=" not in text and "tls_backup.py" not in text
    backup_unit = (ROOT / "deploy/systemd/fire-tls-backup.service").read_text()
    assert "ConditionPathExists=/etc/fire-backend/tls-backup.conf" in backup_unit


@pytest.fixture
def dns_tree(tree, tmp_path, monkeypatch):
    external = tmp_path / "external-letsencrypt"
    original_root = tls.acme_root
    monkeypatch.setattr(
        tls,
        "acme_root",
        lambda values=None: external if tls.adopted(values) else original_root(values),
    )
    tree.update(
        FIREDASH_TLS_MODE="letsencrypt-dns01",
        FIREDASH_TLS_ADOPT_DNS01="yes",
        FIREDASH_TLS_LINEAGE="firedash.example.org",
    )
    tree["FIREDASH_TLS_CERT_PATH"] = str(tls.lineage(tree) / "fullchain.pem")
    tree["FIREDASH_TLS_KEY_PATH"] = str(tls.lineage(tree) / "privkey.pem")
    credentials = tmp_path / "protected-netcup.ini"
    credentials.write_text("synthetic-never-log-this-password")
    credentials.chmod(0o600)
    tree["FIREDASH_TLS_DNS_CREDENTIALS"] = str(credentials)
    monkeypatch.setattr(
        tls,
        "protected_dns_credentials",
        lambda path: tls.require(
            path == credentials and path.read_text() == "synthetic-never-log-this-password",
            "credential changed",
        ),
    )
    monkeypatch.setattr(
        tls,
        "secure_directory",
        lambda path, permissions: path.mkdir(parents=True, mode=permissions, exist_ok=True),
    )
    monkeypatch.setattr(
        tls, "dns_addresses", lambda *a: pytest.fail("CGNAT must not check public addresses")
    )
    monkeypatch.setattr(
        tls, "nginx_apply", lambda *a: pytest.fail("adoption must not replace Nginx site")
    )
    return tree


def dns_lineage(values, pair, *, links=False, version=1):
    source = tls.lineage(values)
    source.mkdir(parents=True, exist_ok=True)
    archive = tls.acme_root(values) / "archive" / tls.cert_name(values)
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"):
        contents = pair[1 if name == "privkey.pem" else 0].read_bytes()
        if links:
            target = archive / name.replace(".pem", f"{version}.pem")
            target.write_bytes(contents)
            if name == "privkey.pem":
                target.chmod(0o600)
            (source / name).unlink(missing_ok=True)
            (source / name).symlink_to(os.path.relpath(target, source))
        else:
            (source / name).write_bytes(contents)
    renewal = tls.acme_root(values) / "renewal"
    renewal.mkdir(parents=True, exist_ok=True)
    (renewal / (tls.cert_name(values) + ".conf")).write_text(
        f"[renewalparams]\nauthenticator = {tls.dns_auth.authenticator(values)}\n"
        f"server = {tls.SERVER}\n"
        f"{tls.dns_auth.credential_parameter(values)} = {values['FIREDASH_TLS_DNS_CREDENTIALS']}\n"
        "dns_netcup_propagation_seconds = 900\n"
    )


def configure_multi(values, monkeypatch, provider="cloudflare"):
    values.update(
        FIREDASH_TLS_DNS_BACKEND="certbot-multi",
        FIREDASH_TLS_DNS_PROVIDER=provider,
        FIREDASH_TLS_DNS_PLUGIN_APPROVED="yes",
    )
    credentials = Path(values["FIREDASH_TLS_DNS_CREDENTIALS"])
    credentials.write_text(
        f"dns_multi_provider = {provider}\n{provider.upper()}_API_TOKEN = synthetic-secret\n"
    )
    monkeypatch.setattr(
        tls,
        "protected_dns_credentials",
        lambda path: tls.require(path == credentials, "credential changed"),
    )
    Path(tls.CERTBOT).with_name("python").touch()


@pytest.mark.parametrize("provider", ["netcup", "cloudflare"])
@pytest.mark.parametrize("failure", [None, "authentication", "propagation", "acme"])
def test_multi_fresh_issuance_retry_and_no_inbound_checks(
    dns_tree, certificate, openssl, monkeypatch, provider, failure, capsys
):
    dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    dns_tree["FIREDASH_TLS_CERT_PATH"], dns_tree["FIREDASH_TLS_KEY_PATH"] = map(str, tls.paths())
    configure_multi(dns_tree, monkeypatch, provider)
    pair = certificate()
    original = tls.run
    calls = []

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        if "plugins" in args:
            return b"* dns-multi\n"
        if "-I" in args:
            return b"5.7.0\n5.6.0\n"
        calls.append(args)
        assert "certonly" in args and "dns-multi" in args
        assert (
            args[args.index("--dns-multi-credentials") + 1]
            == dns_tree["FIREDASH_TLS_DNS_CREDENTIALS"]
        )
        assert "synthetic-secret" not in " ".join(args)
        if failure:
            raise tls.TLSError("sanitized " + failure + " failure")
        dns_lineage(dns_tree, pair)
        return b""

    def deploy(values, **kwargs):
        tls.paths()[0].parent.mkdir(parents=True, exist_ok=True)
        for target, source in zip(tls.paths(), pair, strict=True):
            target.write_bytes(source.read_bytes())

    monkeypatch.setattr(tls, "run", fake)
    monkeypatch.setattr(tls, "deploy", deploy)
    if failure:
        with pytest.raises(tls.TLSError, match="sanitized"):
            tls.prepare(dns_tree)
        assert not tls.paths()[0].exists()
        failure = None
    tls.prepare(dns_tree)
    assert tls.paths()[0].read_bytes() == pair[0].read_bytes()
    assert not tls.WEBROOT.exists()
    assert len(calls) in {1, 2}
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err


@pytest.mark.parametrize("auth", ["dns-netcup", "dns-cloudflare", "dns-multi"])
def test_generic_existing_adoption_preserves_lineage_and_unrelated_files(
    dns_tree, certificate, openssl, monkeypatch, auth
):
    if auth == "dns-multi":
        configure_multi(dns_tree, monkeypatch)
    dns_tree.update(
        FIREDASH_TLS_DNS_BACKEND="certbot-existing",
        FIREDASH_TLS_DNS_AUTHENTICATOR=auth,
        FIREDASH_TLS_DNS_PLUGIN_APPROVED="yes",
    )
    dns_lineage(dns_tree, certificate())
    unrelated = tls.acme_root(dns_tree) / "renewal/other.example.conf"
    unrelated.write_text("untouched foreign certificate bookkeeping")
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tls.acme_root(dns_tree).rglob("*")
        if p.is_file()
    }
    original = tls.run

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        if "-I" in args:
            return b"5.7.0\n5.6.0\n"
        assert "plugins" in args, "healthy adoption must never issue or renew"
        return auth.encode()

    monkeypatch.setattr(tls, "run", fake)
    tls.prepare(dns_tree)
    tls.prepare(dns_tree)
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in before.items())


@pytest.mark.parametrize(
    "changes",
    [
        {"FIREDASH_TLS_DNS_PROVIDER": "exec"},
        {"FIREDASH_TLS_DNS_PROVIDER": "httpreq"},
        {"FIREDASH_TLS_DNS_PROVIDER": "manual"},
        {"FIREDASH_TLS_DNS_PROVIDER": "cloudflare;exec"},
        {"FIREDASH_TLS_DNS_PROVIDER": "unreviewedprovider"},
        {"FIREDASH_TLS_DNS_PLUGIN_APPROVED": "no"},
        {"FIREDASH_TLS_DNS_PROPAGATION_SECONDS": "0"},
        {"FIREDASH_TLS_DNS_PROPAGATION_SECONDS": "1801"},
        {"FIREDASH_TLS_DNS_BACKEND": "certbot-native"},
    ],
)
def test_multi_rejects_invalid_or_unsafe_adapter_settings(dns_tree, monkeypatch, changes):
    configure_multi(dns_tree, monkeypatch)
    dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    dns_tree.update(changes)
    with pytest.raises(tls.TLSError):
        tls.dns_auth.validate(dns_tree, tls.require)


@pytest.mark.parametrize(
    "contents",
    [
        "dns_multi_provider = netcup\nNETCUP_API_KEY = secret\n",
        "dns_multi_provider = cloudflare\nEXEC_PATH = /evil\n",
        "dns_multi_provider = cloudflare\nCLOUDFLARE_API_TOKEN_FILE = /private\n",
        "dns_multi_provider = cloudflare\nCLOUDFLARE_API_TOKEN = a\nCLOUDFLARE_API_TOKEN = b\n",
        "dns_multi_provider = cloudflare\n",
    ],
)
def test_multi_rejects_credential_indirection_and_provider_mismatch(
    dns_tree, monkeypatch, contents
):
    configure_multi(dns_tree, monkeypatch)
    path = Path(dns_tree["FIREDASH_TLS_DNS_CREDENTIALS"])
    path.write_text(contents)
    with pytest.raises(tls.TLSError):
        tls.dns_auth.multi_credentials(path, dns_tree, tls.require)


@pytest.mark.parametrize(
    "output", [b"dns-multi-evil", b"dns-netcup", b"", b"description mentions dns-multi only"]
)
def test_multi_plugin_exact_match(dns_tree, monkeypatch, output):
    configure_multi(dns_tree, monkeypatch)
    dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    with pytest.raises(tls.TLSError, match="plugin missing"):
        tls.dns_auth.check_plugin(dns_tree, [tls.CERTBOT], lambda args: output, tls.require)


@pytest.mark.parametrize("versions", [b"5.8.0\n5.6.0\n", b"5.7.0\n5.5.0\n"])
def test_multi_version_mismatch_fails_before_issuance(dns_tree, monkeypatch, versions):
    configure_multi(dns_tree, monkeypatch)
    dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    with pytest.raises(tls.TLSError, match="version mismatch"):
        tls.dns_auth.check_plugin(
            dns_tree,
            [tls.CERTBOT],
            lambda args: b"dns-multi" if "plugins" in args else versions,
            tls.require,
        )


def test_legacy_backend_cannot_authorize_new_issuance(dns_tree):
    with pytest.raises(tls.TLSError, match="explicit DNS backend"):
        tls.prepare(dns_tree)


@pytest.mark.parametrize(
    "defect", ["hook", "authenticator", "server", "approval", "adoption", "missing-plugin"]
)
def test_generic_adoption_rejects_unapproved_lineage_before_mutation(
    dns_tree, certificate, monkeypatch, defect
):
    dns_tree.update(
        FIREDASH_TLS_DNS_BACKEND="certbot-existing",
        FIREDASH_TLS_DNS_AUTHENTICATOR="dns-cloudflare",
        FIREDASH_TLS_DNS_PLUGIN_APPROVED="yes",
    )
    dns_lineage(dns_tree, certificate())
    path = tls.acme_root(dns_tree) / "renewal" / (tls.cert_name(dns_tree) + ".conf")
    if defect == "hook":
        path.write_text(path.read_text() + "manual_auth_hook = /unreviewed/program\n")
    elif defect == "authenticator":
        path.write_text(path.read_text().replace("dns-cloudflare", "manual"))
    elif defect == "server":
        path.write_text(path.read_text().replace(tls.SERVER, "https://unreviewed.invalid"))
    elif defect == "approval":
        dns_tree["FIREDASH_TLS_DNS_PLUGIN_APPROVED"] = "no"
    elif defect == "adoption":
        dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    before = path.read_bytes()
    monkeypatch.setattr(
        tls, "run", lambda args: b"" if defect == "missing-plugin" else b"dns-cloudflare"
    )
    with pytest.raises(tls.TLSError):
        tls.prepare(dns_tree)
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing", [True, False])
def test_multi_requires_private_credentials_before_issuance(dns_tree, monkeypatch, missing):
    configure_multi(dns_tree, monkeypatch)
    monkeypatch.setattr(tls, "protected_dns_credentials", PROTECT_DNS_CREDENTIALS)
    path = Path(dns_tree["FIREDASH_TLS_DNS_CREDENTIALS"])
    if missing:
        path.unlink()
    else:
        path.chmod(0o666)
    with pytest.raises((OSError, tls.TLSError)):
        tls.dns_auth.issuance_args(dns_tree, tls.protected_dns_credentials, tls.require)


@pytest.mark.parametrize("uid,permissions", [(1, 0o644), (0, 0o666)])
def test_acme_executable_and_ini_reject_unprotected_metadata(uid, permissions):
    class MetadataPath:
        parents = ()

        def resolve(self, **kwargs):
            return self

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | permissions, st_uid=uid)

    with pytest.raises(tls.TLSError, match="protected root-owned"):
        tls.protected_acme_file(MetadataPath())


@pytest.mark.parametrize(
    "diagnostic,category",
    [
        (b"authentication failed synthetic-secret", "DNS provider authentication"),
        (b"incorrect TXT record synthetic-secret", "DNS propagation/validation"),
        (b"HTTP forbidden synthetic-secret", "ACME issuance/renewal"),
        (b"permission denied synthetic-secret", "ACME issuance/renewal"),
    ],
)
def test_sanitized_acme_failure_categories(monkeypatch, capsys, diagnostic, category):
    class Process:
        returncode = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, **kwargs):
            return b"", diagnostic

    monkeypatch.setattr(tls.subprocess, "Popen", lambda *a, **k: Process())
    with pytest.raises(tls.TLSError, match=category) as error:
        tls.run(["certbot", "renew", "--cert-name", "fixture"])
    output = capsys.readouterr()
    assert "synthetic-secret" not in str(error.value) + output.out + output.err


def test_cgnat_adoption_preserves_all_certificate_account_and_credential_bytes(
    dns_tree, certificate, openssl, monkeypatch, capsys
):
    dns_lineage(dns_tree, certificate())
    account = tls.acme_root(dns_tree) / "account-fixture"
    account.write_text("synthetic-account-state")
    before = {p: p.read_bytes() for p in tls.acme_root(dns_tree).rglob("*") if p.is_file()}
    original = tls.run
    calls = []

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        calls.append(args)
        assert "plugins" in args, "healthy adoption must never renew or issue"
        return b"dns-netcup"

    monkeypatch.setattr(tls, "run", fake)
    tls.prepare(dns_tree)
    tls.prepare(dns_tree)
    assert all(path.read_bytes() == value for path, value in before.items())
    assert not tls.WEBROOT.exists()
    assert len(calls) == 2
    output = capsys.readouterr()
    assert "synthetic-never-log-this-password" not in output.out + output.err


def test_cgnat_expired_certificate_recovers_without_inbound_checks(
    dns_tree, certificate, openssl, monkeypatch
):
    dns_lineage(dns_tree, certificate("expired", days=-1))
    replacement = certificate("replacement")
    original = tls.run
    monkeypatch.setattr(
        tls,
        "run",
        lambda args, **kwargs: b"dns-netcup" if "plugins" in args else original(args, **kwargs),
    )
    calls = []

    def renewal(values, **kwargs):
        calls.append(kwargs)
        dns_lineage(values, replacement)

    monkeypatch.setattr(tls, "renew", renewal)
    tls.prepare(dns_tree)
    assert calls == [{"use_hook": False}]
    assert Path(dns_tree["FIREDASH_TLS_CERT_PATH"]).read_bytes() == replacement[0].read_bytes()


@pytest.mark.parametrize("backup_state", ["absent", "invalid"])
def test_repeated_dns_renewal_not_due_never_reloads_or_changes_files(
    dns_tree, certificate, openssl, monkeypatch, backup_state
):
    # Unavailable/misconfigured optional backup state is not a renewal dependency.
    backup_config = tls.ETC / "tls-backup.conf"
    if backup_state == "invalid":
        backup_config.write_text("invalid optional repository configuration")
    dns_lineage(dns_tree, certificate())
    (tls.ETC / "tls").mkdir()
    (tls.ETC / "tls/dns-deployed").write_text(tls.certificate_identity(dns_tree))
    monkeypatch.setattr(tls, "dns_snapshot", lambda values: None)
    original = tls.run
    calls = []

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        if "plugins" in args:
            return b"dns-netcup"
        calls.append(args)
        assert "renew" in args
        return b""

    monkeypatch.setattr(tls, "run", fake)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tls.acme_root(dns_tree).rglob("*")
        if p.is_file()
    }
    tls.renew(dns_tree)
    tls.renew(dns_tree)
    assert len(calls) == 2
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in before.items())
    if backup_state == "invalid":
        assert backup_config.read_text() == "invalid optional repository configuration"
    else:
        assert not backup_config.exists()


@pytest.mark.parametrize(
    "status,allowed",
    [(b"static", True), (b"disabled", True), (b"enabled", False), (b"active", False)],
)
def test_dns_adoption_scheduler_conflicts_fail_closed(tree, monkeypatch, status, allowed):
    tree["FIREDASH_TLS_ADOPT_DNS01"] = "yes"
    monkeypatch.setattr(
        tls.subprocess,
        "run",
        lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": status})(),
    )
    monkeypatch.setattr(tls, "run", lambda args: b"")
    # Only scheduling discovery is mocked; production conflict policy executes.
    monkeypatch.setattr(tls.Path, "rglob", lambda *a, **k: iter(()))
    existing = Path.exists
    monkeypatch.setattr(
        tls.Path, "exists", lambda path: False if str(path) == "/etc/crontab" else existing(path)
    )
    if allowed:
        CHECK_SCHEDULES(tree)
    else:
        with pytest.raises(tls.TLSError, match="competing"):
            CHECK_SCHEDULES(tree)


@pytest.mark.parametrize(
    "uid,gid,permissions,kind",
    [
        (0, 0, 0o644, stat.S_IFREG),
        (1, 0, 0o600, stat.S_IFREG),
        (0, 1, 0o600, stat.S_IFREG),
        (0, 0, 0o600, stat.S_IFLNK),
    ],
)
def test_dns_credential_metadata_rejects_exposure_without_reading_contents(
    uid, gid, permissions, kind
):
    class MetadataPath:
        parents = ()

        def is_absolute(self):
            return True

        def lstat(self):
            return SimpleNamespace(st_mode=kind | permissions, st_uid=uid, st_gid=gid)

    with pytest.raises(tls.TLSError, match="root:root 0600"):
        tls.protected_dns_credentials(MetadataPath())


@pytest.mark.parametrize(
    "contents,permissions", [(b"broken-hook", 0o644), (b"approved-hook", 0o666)]
)
def test_broken_or_writable_hook_is_rejected(contents, permissions):
    class HookPath:
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | permissions, st_uid=0, st_gid=0)

        def read_bytes(self):
            return contents

    with pytest.raises(tls.TLSError):
        tls.verify_owned_file(HookPath(), b"approved-hook")


@LINUX
@pytest.mark.parametrize("failure", [None, "challenge", "mismatch", "nginx", "interrupt"])
def test_dns01_renewal_and_rollback_preserve_authentication(
    dns_tree, certificate, openssl, monkeypatch, failure
):
    dns_lineage(dns_tree, certificate("old"), links=True)
    renewal = tls.acme_root(dns_tree) / "renewal" / (tls.cert_name(dns_tree) + ".conf")
    original_configuration = renewal.read_bytes()
    before = {p.name: os.readlink(p) for p in tls.lineage(dns_tree).iterdir()}
    tls.ETC.joinpath("tls").mkdir()
    next_pair = certificate("next")
    original = tls.run
    calls = []

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        calls.append(args)
        if "plugins" in args:
            return b"dns-netcup"
        if "renew" in args:
            assert args[args.index("--cert-name") + 1] == "firedash.example.org"
            assert "--webroot-path" not in args and "--authenticator" not in args
            if failure == "challenge":
                raise tls.TLSError("failed DNS challenge")
            dns_lineage(dns_tree, next_pair, links=True, version=2)
            if failure == "mismatch":
                (tls.lineage(dns_tree) / "privkey.pem").unlink()
                (tls.lineage(dns_tree) / "privkey.pem").symlink_to(
                    "../../archive/firedash.example.org/privkey1.pem"
                )
            if failure == "interrupt":
                raise InterruptedError("interrupted renewal")
        if args[0] == "nginx" and failure == "nginx":
            raise tls.TLSError("invalid nginx")
        return b""

    monkeypatch.setattr(tls, "run", fake)
    if failure:
        with pytest.raises((tls.TLSError, InterruptedError)):
            tls.renew(dns_tree)
        assert {p.name: os.readlink(p) for p in tls.lineage(dns_tree).iterdir()} == before
        # Reload the old generation even if a candidate reload was interrupted.
        if failure == "nginx":
            assert not any(c[:2] == ["systemctl", "reload"] for c in calls)
            assert (tls.ETC / "tls/dns-recovery.json").exists()
        else:
            assert calls[-1] == ["systemctl", "reload", "nginx"]
    else:
        tls.renew(dns_tree)
        assert Path(dns_tree["FIREDASH_TLS_CERT_PATH"]).read_bytes() == next_pair[0].read_bytes()
        assert calls[-1] == ["systemctl", "reload", "nginx"]
    assert renewal.read_bytes() == original_configuration


@LINUX
@pytest.mark.parametrize("provider", ["netcup", "cloudflare"])
def test_fresh_dns01_uses_no_public_address_or_inbound_probe(
    dns_tree, certificate, openssl, monkeypatch, provider
):
    dns_tree["FIREDASH_TLS_ADOPT_DNS01"] = "no"
    configure_multi(dns_tree, monkeypatch, provider)
    dns_tree["FIREDASH_TLS_CERT_PATH"], dns_tree["FIREDASH_TLS_KEY_PATH"] = map(str, tls.paths())
    pair = certificate()
    original = tls.run

    def fake(args, **kwargs):
        if args[0] == "openssl":
            return original(args, **kwargs)
        if "plugins" in args:
            return b"dns-multi"
        if "-I" in args:
            return b"5.7.0\n5.6.0\n"
        assert "certonly" in args and "dns-multi" in args
        assert "--webroot" not in args
        dns_lineage(dns_tree, pair)
        return b""

    monkeypatch.setattr(tls, "run", fake)
    tls.prepare(dns_tree)
    assert tls.paths()[0].read_bytes() == pair[0].read_bytes()
    assert not tls.WEBROOT.exists()


def _isolated_nginx_routing_config(text, *, port, https_port, access_log):
    # Preserve listener options (especially default_server) from the real template.
    text = re.sub(
        r"(?m)^([ \t]*)listen (80|443)([^;\r\n]*);",
        lambda match: (
            f"{match[1]}listen 127.0.0.1:{port if match[2] == '80' else https_port}{match[3]};"
        ),
        text,
    )
    text = re.sub(r"(?m)^[ \t]*listen \[::\]:(?:80|443)[^;\r\n]*;", "", text)
    return text.replace("/var/log/nginx/fire-backend-access.log", access_log.as_posix())


@pytest.mark.parametrize("final", [False, True])
def test_native_nginx_routing_config_is_isolated(tree, tmp_path, final):
    text = (
        (ROOT / "deploy/nginx/fire-backend.conf").read_text()
        if final
        else tls.challenge_config("firedash.example.org")
    )
    access_log = tmp_path / "access.log"
    rendered = _isolated_nginx_routing_config(
        text, port=18080, https_port=18443, access_log=access_log
    )
    listeners = re.findall(r"\blisten ([^;]+);", rendered)
    assert listeners == (
        ["127.0.0.1:18080 default_server", "127.0.0.1:18443 ssl default_server"]
        if final
        else ["127.0.0.1:18080 default_server"]
    )
    assert "/var/log/" not in rendered
    assert rendered.count("error_log /dev/null crit;") == text.count("error_log /dev/null crit;")
    assert rendered.count("access_log off;") == text.count("access_log off;")
    if final:
        assert f"access_log {access_log.as_posix()} fire_backend_safe;" in rendered
        assert "location ^~ /accounts/setup" in rendered
        assert "location ^~ /reports/access/" in rendered


@pytest.fixture
def synthetic_nginx_upstream():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            result = json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body.decode(),
                    "version": self.request_version,
                    "host": self.headers.get("Host"),
                    "real_ip": self.headers.get("X-Real-IP"),
                    "forwarded_for": self.headers.get("X-Forwarded-For"),
                    "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        do_POST = do_GET

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as upstream:
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            yield upstream.server_port
        finally:
            upstream.shutdown()
            thread.join(timeout=5)


@LINUX
@pytest.mark.skipif(not shutil.which("nginx"), reason="native Nginx binary required")
@pytest.mark.parametrize("final", [False, True])
def test_native_nginx_generated_routing(
    tree, certificate, tmp_path, monkeypatch, final, synthetic_nginx_upstream
):
    """Run actual Nginx on disposable high ports, with the generated location rules."""
    cert, key = certificate()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        https_port = sock.getsockname()[1]
    challenge = tls.WEBROOT / ".well-known/acme-challenge/probe"
    challenge.parent.mkdir(parents=True)
    challenge.write_text("challenge-body")
    if final:
        text = (ROOT / "deploy/nginx/fire-backend.conf").read_text()
        text = (
            text.replace("__SERVER_NAME__", "firedash.example.org")
            .replace("__TLS_CERT_PATH__", str(cert))
            .replace("__TLS_KEY_PATH__", str(key))
            .replace("/var/lib/fire-backend/acme-webroot", str(tls.WEBROOT))
            .replace(
                "server unix:/run/fire-backend/gunicorn.sock;",
                f"server 127.0.0.1:{synthetic_nginx_upstream};",
            )
        )
    else:
        text = tls.challenge_config("firedash.example.org")
    text = _isolated_nginx_routing_config(
        text, port=port, https_port=https_port, access_log=tmp_path / "access.log"
    )
    conf = tmp_path / "nginx.conf"
    (tmp_path / "logs").mkdir()
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log stderr;\nevents {{}}\n"
        f"http {{\naccess_log off;\ninclude {tls.ENABLED.parent}/*;\n}}\n"
    )
    # Disposable Debian-style enabled default: reconciliation must remove only
    # this link before the real routing configuration can validate.
    tls.SITE.parent.mkdir(parents=True)
    tls.ENABLED.parent.mkdir(parents=True)
    available = tls.SITE.parent / "default"
    available.write_text(f"server {{ listen 127.0.0.1:{port} default_server; }}")
    default = tls.ENABLED.parent / "default"
    default.symlink_to(available)
    digest = hashlib.md5(available.read_bytes(), usedforsecurity=False).hexdigest()
    original_run = subprocess.run
    nginx_binary = shutil.which("nginx")
    assert nginx_binary

    def lifecycle_run(args, **kwargs):
        if args[0] == "/usr/bin/dpkg-query":
            return f"installed\n {available} {digest}\n".encode()
        if args == ["nginx", "-t"]:
            result = original_run(
                [nginx_binary, "-p", str(tmp_path), "-c", str(conf), "-t"],
                capture_output=True,
            )
            assert result.returncode == 0, result.stderr.decode()
        else:
            assert args == ["systemctl", "reload", "nginx"]
        return b""

    def is_active(args, **kwargs):
        if args[:2] == ["/usr/bin/systemctl", "is-active"]:
            return SimpleNamespace(returncode=0)
        return original_run(args, **kwargs)

    monkeypatch.setattr(tls, "run", lifecycle_run)
    monkeypatch.setattr(tls.subprocess, "run", is_active)
    tls.nginx_apply(text)
    assert not default.is_symlink() and available.exists()
    nginx = shutil.which("nginx")
    assert nginx
    checked = subprocess.run(
        [nginx, "-p", str(tmp_path), "-c", str(conf), "-t"], capture_output=True, text=True
    )
    assert checked.returncode == 0, f"nginx -t failed:\n{checked.stdout}\n{checked.stderr}"
    process = subprocess.Popen(
        [nginx, "-p", str(tmp_path), "-c", str(conf), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if process.poll() is not None:
                    _, stderr = process.communicate(timeout=10)
                    pytest.fail(f"Nginx exited with {process.returncode}:\n{stderr.decode()}")
                time.sleep(0.05)
        else:
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            pytest.fail(f"Nginx did not listen on disposable port {port}:\n{stderr.decode()}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET", "/.well-known/acme-challenge/probe", headers={"Host": "firedash.example.org"}
        )
        response = conn.getresponse()
        assert response.status == 200 and response.read() == b"challenge-body"
        conn.request("GET", "/ordinary", headers={"Host": "firedash.example.org"})
        response = conn.getresponse()
        assert response.status == (301 if final else 503)
        if final:
            assert response.getheader("Location") == "https://firedash.example.org/ordinary"
        response.read()
        conn.close()
        if final:
            # Native admission checks use only headers and synthetic empty JSON.
            # An interim 100 proves acceptance without allocating a huge body.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            def admission(path, length):
                with socket.create_connection(("127.0.0.1", https_port), timeout=5) as raw:
                    with context.wrap_socket(raw, server_hostname="firedash.example.org") as sock:
                        sock.sendall(
                            (
                                f"POST {path} HTTP/1.1\r\nHost: firedash.example.org\r\n"
                                f"Content-Length: {length}\r\nExpect: 100-continue\r\n\r\n"
                            ).encode()
                        )
                        return int(sock.recv(4096).split(b" ")[1])

            department = "11111111-1111-1111-1111-111111111111"
            boundaries = [
                ("/ordinary", 3 * 1024**2),
                ("/api/v1/tablet/check-in", 64 * 1024),
                ("/api/v1/adoption/preview", 64 * 1024),
                ("/api/v1/adoption/complete", 64 * 1024),
                ("/api/v1/tablet/report-submissions", 96 * 1024**2),
                (f"/departments/{department}/imports/", 300 * 1024**2),
                (f"/departments/{department}/imports/fire-plans/", 300 * 1024**2),
                ("/system/data/dangerous-goods/edit/", 300 * 1024**2),
                (f"/files/{department}/", 300 * 1024**2),
                (f"/files/{department}/explorer/actions/upload/", 300 * 1024**2),
                (f"/files/{department}/explorer/actions/replace/", 300 * 1024**2),
                (f"/departments/{department}/fire-plans/", 101 * 1024**2),
                (f"/departments/{department}/klgv-plans/", 101 * 1024**2),
            ]
            # Actual delivery catches missing proxy content handlers, independently
            # of Nginx's preliminary Expect: 100-continue admission decision.
            for path, _ in boundaries:
                for method in ("GET", "POST"):
                    conn = http.client.HTTPSConnection(
                        "127.0.0.1", https_port, context=context, timeout=5
                    )
                    original_path = path + "?synthetic=original"
                    body = "{}" if method == "POST" else ""
                    conn.request(
                        method,
                        original_path,
                        body,
                        headers={
                            "Host": "firedash.example.org",
                            "X-Real-IP": "192.0.2.1",
                            "X-Forwarded-For": "192.0.2.2",
                            "X-Forwarded-Proto": "http",
                        },
                    )
                    response = conn.getresponse()
                    assert response.status == 200, (method, path)
                    delivered = json.loads(response.read())
                    conn.close()
                    assert delivered == {
                        "method": method,
                        "path": original_path,
                        "body": body,
                        "version": "HTTP/1.1",
                        "host": "firedash.example.org",
                        "real_ip": "127.0.0.1",
                        "forwarded_for": "127.0.0.1",
                        "forwarded_proto": "https",
                    }
            for path, limit in boundaries:
                assert admission(path, limit) == 100, path
                assert admission(path, limit + 1) == 413, path

            statuses = []
            for index in range(45):
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", https_port, context=context, timeout=5
                )
                conn.request(
                    "POST",
                    "/accounts/login/",
                    "{}",
                    headers={"X-Forwarded-For": f"192.0.2.{index}"},
                )
                response = conn.getresponse()
                statuses.append(response.status)
                response.read()
                conn.close()
            assert statuses[0] == 200 and 429 in statuses
            for path in (
                "/accounts/setup/disposable-token/",
                "/api/v1/adoption/preview",
                "/api/v1/adoption/complete",
            ):
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", https_port, context=context, timeout=5
                )
                conn.request("POST", path, "{}")
                response = conn.getresponse()
                assert response.status == 429, path
                response.read()
                conn.close()
            # The exhausted entry bucket never limits ordinary shared-NAT API traffic.
            assert admission("/api/v1/tablet/check-in", 2) == 100
    finally:
        process.terminate()
        process.communicate(timeout=10)


@LINUX
@pytest.mark.parametrize("failure", ["nginx", "reload"])
def test_nginx_site_transaction_restores_existing_site(tree, monkeypatch, failure):
    tls.SITE.parent.mkdir(parents=True)
    tls.ENABLED.parent.mkdir(parents=True)
    tls.SITE.write_text("previous-site")
    tls.ENABLED.symlink_to(tls.SITE)
    unrelated = tls.ENABLED.parent / "unrelated"
    unrelated.write_text("preserved")
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        if (
            args == ["nginx", "-t"]
            and failure == "nginx"
            or args[:2] == ["systemctl", "reload"]
            and failure == "reload"
        ):
            raise tls.TLSError("invalid candidate")
        return b""

    monkeypatch.setattr(tls, "run", fake)
    monkeypatch.setattr(
        tls.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0})()
    )
    with pytest.raises(tls.TLSError):
        tls.nginx_apply("candidate-site")
    assert tls.SITE.read_text() == "previous-site" and unrelated.read_text() == "preserved"
    if failure == "nginx":
        assert not any(c[:2] == ["systemctl", "reload"] for c in calls)


@pytest.mark.parametrize(
    "error",
    [
        OSError("symlink update failed"),
        InterruptedError("reload interrupted"),
        ValueError("bad journal"),
    ],
)
def test_dns_recovery_failure_always_retains_journal(tree, monkeypatch, error):
    journal = tls.ETC / "tls/dns-recovery.json"
    journal.parent.mkdir()
    journal.write_text("private-recovery-metadata")

    def fail(*args):
        raise error

    monkeypatch.setattr(tls, "restore_dns", fail)
    with pytest.raises(tls.TLSError, match="journal retained"):
        tls.recover_dns(tree)
    assert journal.read_text() == "private-recovery-metadata"
    with pytest.raises(tls.TLSError, match="unresolved"):
        tls.dns_snapshot(tree)
    assert journal.read_text() == "private-recovery-metadata"


@LINUX
@pytest.mark.parametrize("failure", ["one-link", "reload", "missing-archive", "manipulated"])
def test_dns_crash_recovery_is_durable_and_repeatable(
    dns_tree, certificate, openssl, monkeypatch, failure
):
    dns_lineage(dns_tree, certificate("before-crash"), links=True)
    (tls.ETC / "tls").mkdir()
    before = tls.certificate_identity(dns_tree)
    journal = tls.dns_snapshot(dns_tree)
    saved = journal.read_bytes()
    dns_lineage(dns_tree, certificate("after-crash"), links=True, version=2)
    original_run, original_replace = tls.run, os.replace
    restored = []
    broken = True

    def replace(source, destination):
        if str(source).endswith(".firedash-chain.pem") and failure == "one-link" and broken:
            raise OSError("synthetic interrupted symlink update")
        original_replace(source, destination)
        if Path(source).name.startswith(".firedash-"):
            restored.append(str(destination))

    def run(args, **kwargs):
        if args[0] == "openssl":
            return original_run(args, **kwargs)
        if failure == "reload" and broken and args[:2] == ["systemctl", "reload"]:
            raise InterruptedError("reload acknowledgement interrupted")
        assert args in (["nginx", "-t"], ["systemctl", "reload", "nginx"])
        return b""

    monkeypatch.setattr(tls.os, "replace", replace)
    monkeypatch.setattr(tls, "run", run)
    missing = tls.acme_root(dns_tree) / "archive" / tls.cert_name(dns_tree) / "privkey1.pem"
    contents = missing.read_bytes()
    if failure == "missing-archive":
        missing.unlink()
    if failure == "manipulated":
        record = json.loads(saved)
        record["links"]["privkey.pem"] = "../../unrelated/secret.pem"
        journal.write_text(json.dumps(record))
    with pytest.raises(tls.TLSError, match="journal retained"):
        tls.recover_dns(dns_tree)
    assert journal.exists()
    if failure in {"missing-archive", "manipulated"}:
        assert not restored
    if failure == "one-link":
        assert restored == [str(tls.lineage(dns_tree) / "cert.pem")]
    broken = False
    missing.write_bytes(contents)
    missing.chmod(0o600)
    journal.write_bytes(saved)
    journal.chmod(0o600)
    tls.recover_dns(dns_tree)
    assert not journal.exists() and tls.certificate_identity(dns_tree) == before
    assert (tls.ETC / "tls/dns-deployed").read_text() == before
    count = len(restored)
    tls.recover_dns(dns_tree)
    assert len(restored) == count


@pytest.fixture
def backup_tree(tree, certificate, backup_module, monkeypatch):
    b = backup_module
    pair = certificate("backup")
    put_lineage(pair)
    archive = tls.acme_root(tree) / "archive/firedash"
    archive.mkdir(parents=True)
    for source in pair:
        shutil.copyfile(source, archive / source.name)
    current = tls.ETC / "tls/current"
    current.mkdir(parents=True)
    for source in pair:
        shutil.copyfile(source, current / source.name)
    account = (
        tls.acme_root(tree)
        / "accounts"
        / "acme-staging-v02.api.letsencrypt.org/directory"
        / ("a" * 32)
    )
    account.mkdir(parents=True)
    for name in ("meta.json", "regr.json", "private_key.json"):
        (account / name).write_text('{"synthetic":"never-log-backup-secret"}')
    renewal = tls.acme_root(tree) / "renewal/firedash.conf"
    renewal.write_text(renewal.read_text() + "account = " + "a" * 32 + "\n")
    (tls.ETC / "install.conf").write_text("".join(f"{k}={v}\n" for k, v in tree.items()))
    b.state().mkdir()
    proof = {
        "packages": {"certbot": "5.7.0", "acme": "5.7.0"},
        "source": "synthetic reviewed fixture",
        "plugin_package": "",
    }
    (b.state() / "plugin-provenance.json").write_text(json.dumps(proof))
    (b.state() / "dependency-lock.txt").write_text("certbot==5.7.0\nacme==5.7.0\n")
    for name in ("repository-password", "repository-auth"):
        (tls.ETC / name).write_text("synthetic-backup-secret")
    (tls.ETC / "tls-backup.conf").write_text(
        "RESTIC_REPOSITORY=rest:https://backup.example.org/firedash-tls\n"
        f"RESTIC_PASSWORD_FILE={(tls.ETC / 'repository-password').as_posix()}\n"
        "RESTIC_REST_USERNAME=firedash-tls\n"
        f"RESTIC_REST_PASSWORD_FILE={(tls.ETC / 'repository-auth').as_posix()}\n"
    )
    modes: dict[str, int] = {}

    def metadata(path):
        actual = path.lstat()
        kind = stat.S_IFMT(actual.st_mode)
        mode = modes.get(
            str(path), 0o777 if path.is_symlink() else 0o700 if path.is_dir() else 0o600
        )
        return SimpleNamespace(st_mode=kind | mode, st_uid=0, st_gid=0)

    def private(path, *, directory=False):
        info = metadata(path)
        assert path.exists()
        tls.require(
            stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
            "private permissions invalid",
        )

    monkeypatch.setattr(b, "metadata", metadata)
    monkeypatch.setattr(b, "private", private)
    monkeypatch.setattr(b, "check_versions", lambda *args: None)
    monkeypatch.setattr(tls, "secure_directory", lambda path, mode: path.mkdir(exist_ok=True))
    return b, tree, modes


def test_tls_backup_exact_encrypted_inputs_exclude_unrelated_and_spool(
    backup_tree, monkeypatch, capsys
):
    b, values, _ = backup_tree
    unrelated = tls.acme_root(values) / "archive/unrelated"
    unrelated.mkdir()
    (unrelated / "privkey.pem").write_text("unrelated-never-backup")
    journal = tls.ETC / "tls/dns-recovery.json"
    journal.write_text('{"retained":"recovery"}')
    calls = []

    def restic(args):
        calls.append(args)
        return json.dumps({"message_type": "summary", "snapshot_id": "b" * 64}).encode()

    monkeypatch.setattr(b, "restic", restic)
    b.backup(values)
    args = calls[0]
    assert args[:4] == ["backup", "--json", "--tag", "firedash-tls"]
    assert str(journal) in args and str(tls.ETC / "install.conf") in args
    assert str(b.state() / "dependency-lock.txt") in args
    assert any("private_key.json" in p for p in args)
    assert not any(
        "unrelated" in p or "/var/spool" in p or "repository-password" in p for p in args
    )
    assert (b.state() / "last-success.json").exists()
    assert "synthetic-backup-secret" not in capsys.readouterr().out


def test_failed_tls_backup_preserves_previous_success(backup_tree, monkeypatch):
    b, values, _ = backup_tree
    previous = b.state() / "last-success.json"
    previous.write_text("previous-success")

    def fail(args):
        raise tls.TLSError("repository unavailable")

    monkeypatch.setattr(b, "restic", fail)
    with pytest.raises(tls.TLSError):
        b.backup(values)
    assert previous.read_text() == "previous-success"


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "permissions",
        "mismatch",
        "account",
        "success",
        "candidate",
        "dns-missing",
        "dns-permissions",
        "dns-success",
    ],
)
def test_isolated_tls_restore_validates_material_and_never_activates(
    backup_tree, openssl, certificate, monkeypatch, tmp_path, problem
):
    b, values, modes = backup_tree
    credential = tls.ETC / "dns.ini"
    if problem.startswith("dns-"):
        values.update(
            FIREDASH_TLS_MODE="letsencrypt-dns01",
            FIREDASH_TLS_DNS_BACKEND="certbot-multi",
            FIREDASH_TLS_DNS_PROVIDER="netcup",
            FIREDASH_TLS_DNS_PLUGIN_APPROVED="yes",
        )
        credential.write_text(
            "dns_multi_provider = netcup\nNETCUP_API_KEY = synthetic-dns-secret\n"
        )
        monkeypatch.setattr(tls, "protected_dns_credentials", b.private)
        renewal = tls.acme_root(values) / "renewal/firedash.conf"
        renewal.write_text(
            renewal.read_text().replace("authenticator = webroot", "authenticator = dns-multi")
            + f"dns_multi_credentials = {credential.as_posix()}\n"
        )
        proof = json.loads((b.state() / "plugin-provenance.json").read_text())
        proof["plugin_package"] = "certbot-dns-multi"
        proof["packages"]["certbot-dns-multi"] = "5.6.0"
        (b.state() / "plugin-provenance.json").write_text(json.dumps(proof))
        (tls.ETC / "install.conf").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    if problem == "candidate":
        pending = tls.ETC / "install.conf.pending"
        (tls.ETC / "install.conf").rename(pending)
        modes[str(pending)] = 0o640
        record = b.manifest(values, pending)
    else:
        record = b.manifest(values)
    root = tmp_path / "restore"
    destination = root / "exercise"
    destination.mkdir(parents=True)
    monkeypatch.setattr(b, "RESTORE_ROOT", root)
    for name, item in record["files"].items():
        source = Path(name)
        target = destination / source.relative_to(source.anchor)
        if item["kind"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            modes[str(target)] = item["mode"]
    manifest_path = destination / (b.state() / "manifest.json").relative_to(tls.ETC.anchor)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(record))
    original = str(next(p for p in record["files"] if p.endswith("private_key.json")))
    if problem.startswith("dns-"):
        original = str(credential)
    target = b.restored_path(destination, original)
    if problem in {"missing", "dns-missing"}:
        target.unlink()
    elif problem in {"permissions", "dns-permissions"}:
        modes[str(target)] = 0o644
    elif problem == "account":
        target.write_text("[]")
        record["files"][str(Path(original))]["sha256"] = hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
        manifest_path.write_text(json.dumps(record))
    elif problem == "mismatch":
        original = values["FIREDASH_TLS_KEY_PATH"]
        target = b.restored_path(destination, original)
        target.write_bytes(certificate("wrong-restored-key")[1].read_bytes())
        record["files"][str(Path(original))]["sha256"] = hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
        manifest_path.write_text(json.dumps(record))
    before = Path(values["FIREDASH_TLS_CERT_PATH"]).read_bytes()
    if problem in {"success", "dns-success", "candidate"}:
        b.restore_check(values, destination)
    else:
        with pytest.raises((tls.TLSError, OSError, AssertionError)):
            b.restore_check(values, destination)
    assert Path(values["FIREDASH_TLS_CERT_PATH"]).read_bytes() == before


def test_restic_transport_has_no_shell_hooks_or_inherited_credentials(backup_tree, monkeypatch):
    b, _, _ = backup_tree
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "unsafe-command")

    def run(args, **kwargs):
        assert args == [b.RESTIC, "--no-cache", "backup", "--", "/selected/file"]
        assert "unsafe-command" not in str(kwargs) and "ambient-secret" not in str(kwargs)
        assert kwargs["env"]["RESTIC_REST_PASSWORD"] == "synthetic-backup-secret"
        return SimpleNamespace(returncode=1, stdout=b"secret raw output", stderr=b"private failure")

    monkeypatch.setattr(b.subprocess, "run", run)
    with pytest.raises(tls.TLSError, match="inspect repository access privately") as error:
        b.restic(["backup", "--", "/selected/file"])
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "destination",
    ["/etc/letsencrypt", "/etc/fire-backend", "/var/lib/fire-backend-tls-restore/../live"],
)
def test_restore_rejects_live_or_traversing_destination(backup_module, destination):
    with pytest.raises(tls.TLSError):
        backup_module.isolated(destination, empty=True)


@pytest.mark.parametrize("backend", ["netcup-existing", "multi"])
def test_tls_backup_includes_selected_dns_credentials_only(backup_tree, monkeypatch, backend):
    b, values, _ = backup_tree
    root = tls.acme_root(values)
    values["FIREDASH_TLS_MODE"] = "letsencrypt-dns01"
    values["FIREDASH_TLS_DNS_PLUGIN_APPROVED"] = "yes"
    if backend == "multi":
        values.update(FIREDASH_TLS_DNS_BACKEND="certbot-multi", FIREDASH_TLS_DNS_PROVIDER="netcup")
        package = "certbot-dns-multi"
        content = "dns_multi_provider = netcup\nNETCUP_API_KEY = synthetic-private-dns\n"
    else:
        values.update(
            FIREDASH_TLS_DNS_BACKEND="certbot-existing",
            FIREDASH_TLS_DNS_AUTHENTICATOR="dns-netcup",
            FIREDASH_TLS_ADOPT_DNS01="yes",
        )
        monkeypatch.setattr(tls, "acme_root", lambda values=None: root)
        package = "certbot-dns-netcup"
        content = "dns_netcup_api_key = synthetic-private-dns\n"
    credential = tls.ETC / "dns.ini"
    credential.write_text(content)
    values["FIREDASH_TLS_DNS_CREDENTIALS"] = str(credential)
    monkeypatch.setattr(tls, "protected_dns_credentials", b.private)
    renewal = root / "renewal/firedash.conf"
    renewal.write_text(
        f"[renewalparams]\nauthenticator = {tls.dns_auth.authenticator(values)}\n"
        f"server = {tls.SERVER}\naccount = "
        + "a" * 32
        + f"\n{tls.dns_auth.credential_parameter(values)} = {credential.as_posix()}\n"
    )
    proof = json.loads((b.state() / "plugin-provenance.json").read_text())
    proof["plugin_package"] = package
    proof["packages"][package] = "5.6.0" if backend == "multi" else "1.0.0"
    (b.state() / "plugin-provenance.json").write_text(json.dumps(proof))
    before = {path: path.read_bytes() for path in (credential, renewal)}
    files = b.inventory(values)
    assert str(credential) in files and b.provenance(values) == proof
    assert all(path.read_bytes() == contents for path, contents in before.items())


@pytest.mark.parametrize("problem", ["none", "stale", "changed", "repository"])
def test_tls_backup_coverage_verifier_detects_stale_or_changed_state(
    backup_tree, monkeypatch, problem
):
    b, values, _ = backup_tree
    monkeypatch.setattr(
        b,
        "restic",
        lambda args: json.dumps({"message_type": "summary", "snapshot_id": "c" * 64}).encode(),
    )
    b.backup(values)
    monkeypatch.setattr(tls, "verify_owned_file", lambda *args: None)
    monkeypatch.setattr(tls, "run", lambda *args, **kwargs: b"reviewed-unit")
    if problem == "stale":
        marker = b.state() / "last-success.json"
        record = json.loads(marker.read_text())
        record["time"] -= 49 * 3600
        marker.write_text(json.dumps(record))
    elif problem == "changed":
        (tls.ETC / "tls/new-recovery-metadata").write_text("new state")
    elif problem == "repository":
        path = tls.ETC / "tls-backup.conf"
        path.write_text(path.read_text().replace("backup.example.org", "other.example.org"))
    if problem == "none":
        b.verify(values)
    else:
        with pytest.raises(tls.TLSError):
            b.verify(values)


def test_tls_backup_dependency_versions_are_checked_without_plugin_import(backup_tree, monkeypatch):
    b, values, _ = backup_tree
    # Restore the real verifier rather than the filesystem fixture boundary.
    spec = importlib.util.spec_from_file_location(
        "backup_versions", ROOT / "deploy/lib/tls_backup.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "tls", tls)
    binary = Path(tls.certbot_args(values)[0])
    binary.write_text("#!/opt/reviewed/bin/python3\n")
    expected = {"certbot": "5.7.0", "acme": "5.7.0"}

    def run(args, **kwargs):
        assert args[:3] == [str(Path("/opt/reviewed/bin/python3")), "-I", "-c"]
        assert "import_module" not in args[3] and "json.load(sys.stdin)" in args[3]
        assert all(isinstance(name, str) for name in json.loads(kwargs["input"]))
        return json.dumps(expected).encode()

    monkeypatch.setattr(tls, "run", run)
    module.check_versions(values, expected)
    with pytest.raises(tls.TLSError, match="recorded"):
        module.check_versions(values, {"certbot": "old"})


@LINUX
@pytest.mark.skipif(shutil.which("restic") is None, reason="native Restic executable unavailable")
def test_native_restic_encrypted_roundtrip_preserves_relative_links(
    backup_tree, openssl, monkeypatch, tmp_path
):
    b, values, _ = backup_tree
    current = tls.ETC / "tls/current"
    generation = current.with_name("verified")
    current.rename(generation)
    current.symlink_to("verified")
    repository = tmp_path / "disposable-repository"
    password = tmp_path / "disposable-password"
    password.write_text("synthetic-restic-encryption-passphrase")
    environment = {
        "PATH": os.environ["PATH"],
        "RESTIC_REPOSITORY": str(repository),
        "RESTIC_PASSWORD_FILE": str(password),
    }
    executable = shutil.which("restic")
    assert executable

    def local_restic(args):
        return subprocess.run(
            [executable, "--no-cache", *args],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=90,
        ).stdout

    local_restic(["init"])
    monkeypatch.setattr(b, "restic", local_restic)
    b.backup(values)
    snapshot = json.loads((b.state() / "last-success.json").read_text())["snapshot"]
    root = tmp_path / "native-restore"
    destination = root / "exercise"
    destination.mkdir(parents=True)
    monkeypatch.setattr(b, "RESTORE_ROOT", root)
    b.restore(values, snapshot, destination)
    restored = b.restored_path(destination, str(current))
    assert restored.is_symlink() and os.readlink(restored) == "verified"
    assert (
        b"PRIVATE KEY" in b.restored_path(destination, values["FIREDASH_TLS_KEY_PATH"]).read_bytes()
    )
    for path in repository.rglob("*"):
        if path.is_file():
            assert b"never-log-backup-secret" not in path.read_bytes()
            assert b"BEGIN PRIVATE KEY" not in path.read_bytes()
    with pytest.raises(tls.TLSError, match="empty"):
        b.restore(values, snapshot, destination)


@LINUX
def test_backup_private_boundary_rejects_actual_public_file(backup_module, tmp_path):
    path = tmp_path / "public-credential"
    path.write_text("synthetic")
    path.chmod(0o644)
    with pytest.raises(backup_module.config.ConfigurationError):
        backup_module.private(path)


def test_unresolved_journal_blocks_new_renewal_without_certbot(dns_tree, certificate, monkeypatch):
    dns_lineage(dns_tree, certificate())
    journal = tls.ETC / "tls/dns-recovery.json"
    journal.parent.mkdir()
    journal.write_text("unresolved-private-journal")
    monkeypatch.setattr(tls.dns_auth, "check_plugin", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tls, "run", lambda *args, **kwargs: pytest.fail("unresolved recovery must prevent Certbot")
    )
    for _ in range(2):
        with pytest.raises(tls.TLSError, match="journal retained"):
            tls.renew(dns_tree)
        assert journal.read_text() == "unresolved-private-journal"


@pytest.mark.parametrize("problem", ["missing", "unavailable", "invalid", "none"])
def test_tls_backup_preflight_is_read_only_and_checks_repository(backup_tree, monkeypatch, problem):
    b, values, _ = backup_tree
    if problem == "missing":
        (tls.ETC / "tls-backup.conf").unlink()
    calls = []

    def restic(args):
        calls.append(args)
        if problem == "unavailable":
            raise tls.TLSError("TLS encrypted repository unavailable")
        return json.dumps(
            {"version": 2, "id": "a" * 64 if problem != "invalid" else "bad"}
        ).encode()

    monkeypatch.setattr(b, "restic", restic)
    before = {p: p.read_bytes() for p in tls.ETC.rglob("*") if p.is_file()}
    if problem == "none":
        b.preflight(values)
    else:
        with pytest.raises((tls.TLSError, FileNotFoundError)):
            b.preflight(values)
    assert before == {p: p.read_bytes() for p in tls.ETC.rglob("*") if p.is_file()}
    assert calls == ([] if problem == "missing" else [["cat", "config"]])


def test_initial_snapshot_covers_candidate_before_live_configuration(backup_tree, monkeypatch):
    b, values, modes = backup_tree
    installed = tls.ETC / "install.conf"
    pending = tls.ETC / "install.conf.pending"
    installed.rename(pending)
    modes[str(pending)] = 0o640
    calls = []

    def restic(args):
        calls.append(args)
        return json.dumps({"message_type": "summary", "snapshot_id": "c" * 64}).encode()

    monkeypatch.setattr(b, "restic", restic)
    b.backup(values, pending)
    saved = json.loads((b.state() / "manifest.json").read_text())
    assert saved["configuration"] == str(pending)
    assert str(pending) in calls[0] and str(installed) not in calls[0]
    assert not installed.exists()
    assert json.loads((b.state() / "last-success.json").read_text())["snapshot"] == "c" * 64


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o660])
def test_backup_installation_metadata_permission_contract(backup_tree, mode):
    b, _, modes = backup_tree
    path = tls.ETC / "install.conf"
    modes[str(path)] = mode
    if mode in {0o600, 0o640}:
        b.installation_file(path)
    else:
        with pytest.raises(tls.TLSError):
            b.installation_file(path)


@pytest.mark.parametrize("problem", ["wrong-inode", "unlocked", "other-owner", "none"])
def test_inherited_backup_lock_requires_installer_ownership(
    tree, backup_module, monkeypatch, problem
):
    b = backup_module
    tls.LOCK.touch()
    info = tls.LOCK.stat()
    monkeypatch.setattr(
        b.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=0,
            st_dev=info.st_dev,
            st_ino=info.st_ino + (problem == "wrong-inode"),
        ),
    )
    calls = []

    def flock(fd, operation):
        calls.append(fd)
        if len(calls) == 1 and problem != "unlocked":
            raise BlockingIOError("separate open excluded")
        if len(calls) == 2 and problem == "other-owner":
            raise BlockingIOError("fd 9 is not owner")

    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock))
    entered = False
    if problem == "none":
        with b.operation_lock(True):
            entered = True
    else:
        with pytest.raises((tls.TLSError, BlockingIOError)), b.operation_lock(True):
            entered = True
    assert entered == (problem == "none")
    if problem == "none":
        assert calls[-1] == 9 and len(calls) == 2  # Never unlock parent's descriptor.


@LINUX
@pytest.mark.parametrize("failure", [None, "validate", "reload", "activate"])
def test_packaged_default_transaction(tree, monkeypatch, failure):
    tls.SITE.parent.mkdir(parents=True)
    tls.ENABLED.parent.mkdir(parents=True)
    available = tls.SITE.parent / "default"
    available.write_text("server { listen 80 default_server; listen [::]:80 default_server; }")
    default = tls.ENABLED.parent / "default"
    default.symlink_to("../available/default")
    unrelated = tls.ENABLED.parent / "other"
    unrelated.write_text("server { listen 8080 default_server; }")
    digest = hashlib.md5(available.read_bytes(), usedforsecurity=False).hexdigest()
    metadata = f"installed\n {available} {digest}\n".encode()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "/usr/bin/dpkg-query":
            return metadata
        assert not default.exists() or tls.SITE.read_text() == "old"
        if args == ["nginx", "-t"] and failure == "validate":
            raise tls.TLSError("validation failed")
        if args[:2] == ["systemctl", "reload"] and failure == "reload":
            raise tls.TLSError("reload failed")
        if args[:2] == ["systemctl", "enable"] and failure == "activate":
            raise tls.TLSError("activation failed")
        return b""

    monkeypatch.setattr(tls, "run", run)
    monkeypatch.setattr(
        tls.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=3 if failure == "activate" else 0),
    )
    tls.SITE.write_text("old")
    if failure:
        with pytest.raises(tls.TLSError):
            tls.nginx_apply("candidate")
        assert tls.SITE.read_text() == "old"
        assert os.readlink(default) == "../available/default"
        assert not tls.ENABLED.exists()
    else:
        tls.nginx_apply("candidate")
        assert not default.is_symlink()
        tls.nginx_apply("candidate")
        assert tls.ENABLED.resolve() == tls.SITE
        assert tls.SITE.read_text() == "candidate"
    assert available.read_text().startswith("server { listen 80")
    assert unrelated.read_text() == "server { listen 8080 default_server; }"


@LINUX
@pytest.mark.parametrize("state", ["modified", "file", "foreign", "unowned", "removed", "custom"])
def test_default_site_ownership_rejects_custom_state(tree, monkeypatch, state):
    tls.SITE.parent.mkdir(parents=True)
    tls.ENABLED.parent.mkdir(parents=True)
    available = tls.SITE.parent / "default"
    available.write_text("packaged default")
    digest = hashlib.md5(available.read_bytes(), usedforsecurity=False).hexdigest()
    default = tls.ENABLED.parent / "default"
    if state == "file":
        default.write_text("administrator default")
    else:
        default.symlink_to(available if state != "foreign" else tls.SITE.parent / "foreign")
    if state == "modified":
        available.write_text("modified default")
    if state == "custom":
        (tls.ENABLED.parent / "custom").write_text("server { listen [::]:80 default_server; }")
    metadata = f"installed\n {available} {digest}\n".encode()
    if state == "unowned":
        metadata = b"installed\n"
    if state == "removed":
        metadata = metadata.replace(b"installed", b"config-files")
    monkeypatch.setattr(tls, "run", lambda args, **kw: metadata)
    with pytest.raises(tls.TLSError, match="manually"):
        tls.nginx_ownership_preflight()
    assert default.exists() or default.is_symlink()
    assert not tls.SITE.exists()


def test_optional_legacy_pdf_path():
    unit = (ROOT / "deploy/systemd/fire-pdf-sanitizer@.service").read_text()
    match = re.search(r"^InaccessiblePaths=(.*)$", unit, re.M)
    assert match
    paths = match.group(1).split()
    assert "-/var/lib/fire-backend/publications-tmp" in paths
    assert "/var/lib/fire-backend/publications-tmp" not in paths
    assert "/var/lib/fire-backend/publications" in paths
    assert "/var/lib/fire-backend/fire-plans" in paths
    assert "/etc/fire-backend" in paths and "/srv/firedash" in paths


@LINUX
@pytest.mark.skipif(
    not Path("/etc/nginx/sites-available/default").is_file(),
    reason="installed Debian nginx-common conffile required (read-only)",
)
def test_native_installed_package_identity_with_disposable_copy(tree, monkeypatch):
    source = Path("/etc/nginx/sites-available/default")
    metadata = tls.run(
        ["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Status}\n${Conffiles}", "nginx-common"]
    )
    tls.SITE.parent.mkdir(parents=True)
    tls.ENABLED.parent.mkdir(parents=True)
    available = tls.SITE.parent / "default"
    available.write_bytes(source.read_bytes())
    enabled = tls.ENABLED.parent / "default"
    enabled.symlink_to(available)
    metadata = metadata.replace(str(source).encode(), str(available).encode())
    monkeypatch.setattr(tls, "run", lambda args: metadata)
    expected = re.search(rb" " + re.escape(str(available).encode()) + rb" ([a-f0-9]{32})", metadata)
    assert expected
    if hashlib.md5(source.read_bytes(), usedforsecurity=False).hexdigest().encode() == expected[1]:
        assert tls.packaged_default_site() == enabled
    else:
        with pytest.raises(tls.TLSError):
            tls.packaged_default_site()
    available.write_bytes(available.read_bytes() + b"\n# administrator modification\n")
    with pytest.raises(tls.TLSError):
        tls.packaged_default_site()
    assert enabled.is_symlink()


def test_disabled_default_needs_no_package_query(tree, monkeypatch):
    tls.SITE.parent.mkdir(parents=True)
    (tls.SITE.parent / "default").write_text("administrator-modified but disabled")
    monkeypatch.setattr(
        tls, "run", lambda *a, **k: pytest.fail("no enabled default needs ownership query")
    )
    assert tls.nginx_ownership_preflight() is None


@pytest.mark.parametrize(
    "listener",
    [
        "80 default_server",
        "[::]:80 default_server",
        "443 ssl default_server",
        "*:80 default_server",
    ],
)
def test_custom_default_listener_preflight_is_read_only(tree, listener):
    tls.ENABLED.parent.mkdir(parents=True)
    custom = tls.ENABLED.parent / "administrator"
    text = f"server {{ listen {listener}; }}"
    custom.write_text(text)
    with pytest.raises(tls.TLSError, match="manually"):
        tls.nginx_ownership_preflight()
    assert custom.read_text() == text
    assert not tls.SITE.exists()


@pytest.mark.parametrize("listener", ["8080 default_server", "192.0.2.5:80 default_server", "80"])
def test_nonconflicting_virtual_host_preflight_preserves_configuration(tree, listener):
    tls.ENABLED.parent.mkdir(parents=True)
    custom = tls.ENABLED.parent / "administrator"
    text = f"server {{ listen {listener}; }}"
    custom.write_text(text)
    assert tls.nginx_ownership_preflight() is None
    assert custom.read_text() == text
