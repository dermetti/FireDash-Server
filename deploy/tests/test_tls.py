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
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def test_final_https_render_and_activation_marker(tree, certificate, openssl, monkeypatch):
    pair = certificate()
    tls.paths()[0].parent.mkdir(parents=True)
    for source, destination in zip(pair, tls.paths(), strict=True):
        shutil.copyfile(source, destination)
    monkeypatch.setattr(tls.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(tls.sys, "argv", ["tls.py", "nginx"])
    monkeypatch.setattr(tls, "settings", lambda: tree)
    rendered: list[str] = []
    monkeypatch.setattr(tls, "nginx_apply", rendered.append)
    tls.main()
    expected = (ROOT / "deploy/nginx/fire-backend.conf").read_text()
    expected = (
        expected.replace("__SERVER_NAME__", "firedash.example.org")
        .replace("__TLS_CERT_PATH__", tree["FIREDASH_TLS_CERT_PATH"])
        .replace("__TLS_KEY_PATH__", tree["FIREDASH_TLS_KEY_PATH"])
    )
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


def test_repeated_dns_renewal_not_due_never_reloads_or_changes_files(
    dns_tree, certificate, openssl, monkeypatch
):
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
        assert not any(c[:2] == ["systemctl", "reload"] for c in calls)
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


@LINUX
@pytest.mark.skipif(not shutil.which("nginx"), reason="native Nginx binary required")
@pytest.mark.parametrize("final", [False, True])
def test_native_nginx_generated_routing(tree, certificate, tmp_path, final):
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
        )
    else:
        text = tls.challenge_config("firedash.example.org")
    text = (
        text.replace("listen 80;", f"listen 127.0.0.1:{port};")
        .replace("listen [::]:80;", "")
        .replace("listen 443 ssl;", f"listen 127.0.0.1:{https_port} ssl;")
        .replace("listen [::]:443 ssl;", "")
    )
    conf = tmp_path / "nginx.conf"
    (tmp_path / "logs").mkdir()
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log stderr;\nevents {{}}\n"
        f"http {{\naccess_log off;\n{text}\n}}\n"
    )
    nginx = shutil.which("nginx")
    assert nginx
    subprocess.run(
        [nginx, "-p", str(tmp_path), "-c", str(conf), "-t"], check=True, capture_output=True
    )
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
                time.sleep(0.05)
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
