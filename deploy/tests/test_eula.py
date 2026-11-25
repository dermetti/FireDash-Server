"""EULA verification boundaries, offline transport and optional real Minisign tests."""

import base64
import copy
import importlib.util
import json
import os
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deployment_eula", ROOT / "deploy/lib/eula.py")
assert spec is not None and spec.loader is not None
e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e
spec.loader.exec_module(e)


class OfflineTransport:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, url, limit):
        self.calls.append((url, limit))
        if url not in self.payloads:
            raise e.EULAError("Provider document or detached signature missing")
        return self.payloads[url]


@pytest.fixture
def release(tmp_path, monkeypatch):
    legal = tmp_path / "legal"
    legal.mkdir()
    manifest = json.loads((ROOT / "legal/provider-manifest.json").read_bytes())
    key_id = "0123456789ABCDEF"
    key = (
        b"untrusted comment: synthetic test key\n"
        + base64.b64encode(b"Ed" + bytes.fromhex(key_id)[::-1] + bytes(range(32)))
        + b"\n"
    )
    manifest["key_id"] = key_id
    payloads = {}
    authentic = set()
    for language, record in manifest["languages"].items():
        template = f"Public template {language}\n".encode()
        provider = f"Synthetic provider document {language}\n".encode()
        signature = f"synthetic detached signature {language}\n".encode()
        (legal / record["template"]).write_bytes(template)
        record["template_sha256"] = e.sha256(template)
        record["provider_sha256"] = e.sha256(provider)
        payloads[record["provider_url"]] = provider
        payloads[record["signature_url"]] = signature
        authentic.add((provider, signature, key))
    (legal / "provider-signing.pub").write_bytes(key)
    transport = OfflineTransport(payloads)
    scratch: set[Path] = set()
    verified = []
    original_directory = e.tempfile.TemporaryDirectory

    def temporary_directory(**kwargs):
        result = original_directory(dir=tmp_path, **kwargs)
        scratch.add(Path(result.name))
        return result

    monkeypatch.setattr(e.tempfile, "TemporaryDirectory", temporary_directory)

    def run(args, *, timeout, stdout=subprocess.PIPE):
        assert timeout in (5, 15)
        assert stdout == subprocess.PIPE
        if args[1:] == ["-v"]:
            return SimpleNamespace(returncode=0, stdout=b"minisign synthetic", stderr=b"")
        assert args[1:3] == ["-V", "-q"]
        paths = [Path(args[args.index(flag) + 1]) for flag in ("-m", "-x", "-p")]
        scratch.update(path.parent for path in paths)
        if os.name == "posix":
            for path in paths:
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        inputs = tuple(path.read_bytes() for path in paths)
        verified.append(inputs)
        return SimpleNamespace(
            returncode=0 if inputs in authentic else 1, stdout=b"", stderr=b"not relayed"
        )

    monkeypatch.setattr(e, "_run", run)
    case = SimpleNamespace(
        legal=legal,
        manifest=manifest,
        key=key,
        transport=transport,
        authentic=authentic,
        scratch=scratch,
        verified=verified,
    )

    def save():
        (legal / "provider-manifest.json").write_bytes(json.dumps(manifest).encode())

    case.save = save
    save()
    yield case
    assert all(not path.exists() for path in scratch)


def verify(case):
    return e.verify_document_set(case.legal, transport=case.transport, minisign=Path("synthetic"))


def test_both_languages_are_verified_and_return_original_immutable_bytes(release):
    before = {path.name: path.read_bytes() for path in release.legal.iterdir()}
    result = verify(release)
    assert [document.language for document in result.documents] == ["de", "en"]
    assert len(release.verified) == 2
    assert len(release.transport.calls) == 4
    assert [limit for _, limit in release.transport.calls] == [65536, 8192, 65536, 8192]
    for document, record in zip(result.documents, result.manifest.languages, strict=True):
        assert document.provider == release.transport.payloads[record.provider_url]
        assert document.template == before[record.template]
        assert document.signature == release.transport.payloads[record.signature_url]
    assert result.manifest_sha256 == e.sha256(before["provider-manifest.json"])
    assert result.public_key_sha256 == e.sha256(release.key)
    with pytest.raises(FrozenInstanceError):
        result.manifest.agreement_revision = 999
    assert {path.name: path.read_bytes() for path in release.legal.iterdir()} == before


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "modified",
        "wrong_key",
        "hash",
        "swapped_documents",
        "swapped_signatures",
        "swapped_pairs",
    ],
)
def test_signature_hash_and_language_fail_closed(release, change):
    de = release.manifest["languages"]["de"]
    en = release.manifest["languages"]["en"]
    payloads = release.transport.payloads
    if change == "signature":
        payloads[de["signature_url"]] = b"invalid signature"
    elif change == "modified":
        payloads[de["provider_url"]] += b"modified"
    elif change == "wrong_key":
        # Keep the format and key ID valid, but change the public key itself.
        raw = base64.b64decode(release.key.splitlines()[1])
        wrong = release.key.splitlines()[0] + b"\n" + base64.b64encode(raw[:-1] + b"x") + b"\n"
        (release.legal / "provider-signing.pub").write_bytes(wrong)
    elif change == "hash":
        de["provider_sha256"] = "0" * 64
    else:
        fields = {
            "swapped_documents": ["provider_url"],
            "swapped_signatures": ["signature_url"],
            "swapped_pairs": ["provider_url", "signature_url"],
        }[change]
        for name in fields:
            payloads[de[name]], payloads[en[name]] = payloads[en[name]], payloads[de[name]]
    release.save()
    with pytest.raises(e.EULAError, match="signature|SHA-256"):
        verify(release)


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("field", ["provider_url", "signature_url"])
def test_missing_inputs_never_return_partial_results(release, language, field):
    del release.transport.payloads[release.manifest["languages"][language][field]]
    with pytest.raises(e.EULAError, match="missing"):
        verify(release)
    if language == "en":
        assert len(release.verified) == 1  # German passed, but nothing was returned.


@pytest.mark.parametrize("language", ["de", "en"])
def test_modified_template_is_rejected_before_network(release, language):
    (release.legal / f"EULA.public.{language}.md").write_bytes(b"modified template")
    with pytest.raises(e.EULAError, match="Public template integrity"):
        verify(release)
    assert release.transport.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        b"\xff",
        b"[]",
        b"null",
        b'{"schema_version":1,"schema_version":1}',
        b'{"languages":{"de":{},"de":{}}}',
        b'{"schema_version":NaN}',
        b"[" * 1100,
    ],
)
def test_malformed_manifests_and_duplicate_keys(payload):
    with pytest.raises(e.EULAError, match="Invalid manifest"):
        e.parse_manifest(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("schema_version", "1"),
        ("agreement_revision", 0),
        ("agreement_revision", True),
        ("agreement_revision", "2"),
        ("provider_revision", -1),
        ("provider_revision", 1.0),
        ("agreement_version", "../1.0-beta"),
        ("agreement_version", "1.0-beta/extra"),
        ("effective_date", "2026-02-30"),
        ("effective_date", "20261125"),
        ("effective_date", None),
        ("status", "draft"),
        ("status", True),
        ("authoritative_language", "en"),
        ("key_id", "untrusted comment"),
        ("languages", {"de": {}}),
        ("languages", []),
    ],
)
def test_invalid_metadata(release, field, value):
    release.manifest[field] = value
    with pytest.raises(e.EULAError, match="Invalid manifest"):
        e.parse_manifest(json.dumps(release.manifest).encode())


@pytest.mark.parametrize(
    "change", ["missing", "unknown", "extra_language", "unknown_language_field"]
)
def test_schema_fields_are_exact(release, change):
    manifest = release.manifest
    if change == "missing":
        del manifest["key_id"]
    elif change == "unknown":
        manifest["extra"] = "unsupported"
    elif change == "extra_language":
        manifest["languages"]["fr"] = {}
    else:
        manifest["languages"]["de"]["command"] = "do not execute"
    with pytest.raises(e.EULAError, match="Invalid manifest"):
        e.parse_manifest(json.dumps(manifest).encode())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("template", "../EULA.public.de.md"),
        ("template", "/etc/passwd"),
        ("template", "EULA.public.en.md"),
        ("template", "EULA.public.de.md\x00"),
        ("template_sha256", "A" * 64),
        ("provider_sha256", "g" * 64),
        ("provider_sha256", 1),
        ("provider_sha256", "1" * 63),
        ("provider_url", "http://firedash-web.mjblab.de/legal/1.0-beta/PROVIDER.de.md"),
        ("provider_url", "https://evil.invalid/legal/1.0-beta/PROVIDER.de.md"),
        ("provider_url", "https://firedash-web.mjblab.de@evil.invalid/PROVIDER.de.md"),
        ("provider_url", "https://firedash-web.mjblab.de:443/legal/1.0-beta/PROVIDER.de.md"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/../PROVIDER.de.md"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/%50ROVIDER.de.md"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.en.md"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.de.md?tracking=x"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.de.md#fragment"),
        ("provider_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.de.md\n"),
        ("provider_url", "$(command)"),
        ("provider_url", None),
        ("signature_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.en.md.minisig"),
        ("signature_url", e.ORIGIN + "/legal/1.0-beta/PROVIDER.de.md"),
    ],
)
def test_unsafe_paths_urls_hashes_and_swapped_manifest_languages(release, field, value):
    release.manifest["languages"]["de"][field] = value
    with pytest.raises(e.EULAError, match="Invalid manifest"):
        e.parse_manifest(json.dumps(release.manifest).encode())


def test_reviewed_future_revisions_and_dates_are_not_rejected(release):
    release.manifest.update(agreement_revision=3, provider_revision=2, effective_date="2099-12-31")
    assert e.parse_manifest(json.dumps(release.manifest).encode()).agreement_revision == 3
    release.manifest["agreement_version"] = "2.0"
    for record in release.manifest["languages"].values():
        for name in ("provider_url", "signature_url"):
            record[name] = record[name].replace("1.0-beta", "2.0")
    assert e.parse_manifest(json.dumps(release.manifest).encode()).agreement_version == "2.0"


@pytest.mark.parametrize(
    "provider",
    [
        b"\xff",
        b"text\x00",
        b"text\x1b",
        b"text\x7f",
        b"text\xc2\x85",
        "text\u202e".encode(),
        b" \n",
    ],
)
def test_signed_pinned_but_unsafe_provider_text_is_rejected(release, provider):
    record = release.manifest["languages"]["de"]
    signature = release.transport.payloads[record["signature_url"]]
    release.transport.payloads[record["provider_url"]] = provider
    release.authentic.add((provider, signature, release.key))
    record["provider_sha256"] = e.sha256(provider)
    release.save()
    with pytest.raises(e.EULAError, match="UTF-8|control|empty"):
        verify(release)


@pytest.mark.parametrize(
    ("field", "content"),
    [
        ("provider_url", b"x" * 65537),
        ("signature_url", b"x" * 8193),
        ("signature_url", b""),
        ("provider_url", b""),
    ],
    ids=["oversized-provider", "oversized-signature", "empty-signature", "empty-provider"],
)
def test_transport_injection_cannot_bypass_size_or_empty_checks(release, field, content):
    release.transport.payloads[release.manifest["languages"]["de"][field]] = content
    with pytest.raises(e.EULAError, match="missing or oversized"):
        verify(release)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_cleanup_on_unexpected_exception(release, failure):
    original = release.transport.fetch

    def fetch(url, limit):
        if ".en." in url:
            raise failure("synthetic interruption")
        return original(url, limit)

    release.transport.fetch = fetch
    with pytest.raises(failure):
        verify(release)


def test_cleanup_and_signal_handler_restoration_on_termination(release):
    previous = signal.getsignal(signal.SIGTERM)
    original = release.transport.fetch

    def fetch(url, limit):
        if ".en." in url:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return original(url, limit)

    release.transport.fetch = fetch
    with pytest.raises(e.EULAError, match="interrupted"):
        verify(release)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_incorrect_public_key_id_fails_before_network(release):
    release.manifest["key_id"] = "F" * 16
    release.save()
    with pytest.raises(e.EULAError, match="Incorrect public key"):
        verify(release)
    assert release.transport.calls == []


def test_missing_release_file(release):
    (release.legal / "provider-signing.pub").unlink()
    with pytest.raises(e.EULAError, match="missing or unreadable"):
        verify(release)


@pytest.mark.skipif(
    os.name == "nt", reason="Native symlink creation privileges unavailable on Windows"
)
def test_release_input_symlink_rejected(release):
    path = release.legal / "EULA.public.de.md"
    content = path.read_bytes()
    path.unlink()
    target = release.legal / "synthetic-target"
    target.write_bytes(content)
    path.symlink_to(target)
    with pytest.raises(e.EULAError, match="symlinks"):
        verify(release)


@pytest.fixture
def curl_runner(monkeypatch, tmp_path):
    state = SimpleNamespace(
        code=0,
        status=b"200",
        content=b"synthetic body",
        calls=[],
        version=b"curl 8.4.0 synthetic",
        paths=[],
    )

    def run(args, **kwargs):
        state.calls.append((args, kwargs))
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout=state.version, stderr=b"")
        output = kwargs["stdout"]
        output.write(state.content)
        return SimpleNamespace(returncode=state.code, stdout=None, stderr=state.status)

    original = e.tempfile.TemporaryDirectory

    def temporary_directory(**kwargs):
        result = original(dir=tmp_path, **kwargs)
        state.paths.append(Path(result.name))
        return result

    monkeypatch.setattr(e.subprocess, "run", run)
    monkeypatch.setattr(e.tempfile, "TemporaryDirectory", temporary_directory)
    yield state
    assert all(not path.exists() for path in state.paths)


URL = e.ORIGIN + "/legal/1.0-beta/PROVIDER.de.md"


def test_curl_security_arguments_and_clean_environment(curl_runner, monkeypatch):
    monkeypatch.setenv("CURL_CA_BUNDLE", "/untrusted/ca")
    monkeypatch.setenv("HTTPS_PROXY", "https://sensitive:password@untrusted.invalid")
    transport = e.CurlTransport(Path("synthetic-curl"))
    assert transport.fetch(URL, e.DOCUMENT_LIMIT) == curl_runner.content
    args, kwargs = curl_runner.calls[-1]
    assert args[:2] == ["synthetic-curl", "-q"]
    for name, value in (
        ("--proto", "=https"),
        ("--proto-redir", "=https"),
        ("--connect-timeout", "10"),
        ("--max-time", "30"),
        ("--max-filesize", "65536"),
        ("--retry", "0"),
        ("--max-redirs", "0"),
        ("--proxy", ""),
        ("--url", URL),
    ):
        assert args[args.index(name) + 1] == value
    assert {"--no-location", "--no-insecure", "--no-netrc", "--fail"} <= set(args)
    assert "-k" not in args and "--location" not in args and "--config" not in args
    assert kwargs["timeout"] == 35
    assert "CURL_CA_BUNDLE" not in kwargs["env"] and "HTTPS_PROXY" not in kwargs["env"]
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    ("code", "status", "message", "requests"),
    [
        (0, b"302", "redirect", 1),
        (0, b"307", "redirect", 1),
        (22, b"403", "HTTP 403.*WAF", 1),
        (22, b"404", "missing", 1),
        (22, b"503", "HTTP 503", 1),
        (0, b"204", "HTTP 204", 1),
        (0, b"000", "confirm HTTP 200", 1),
        (0, b"invalid status", "confirm HTTP 200", 1),
        (60, b"000", "TLS certificate", 1),
        (28, b"000", "network timeout", 2),
        (63, b"200", "size limit", 1),
        (18, b"200", "incomplete or truncated", 1),
        (56, b"200", "incomplete or truncated", 2),
        (7, b"000", "connectivity", 2),
    ],
)
def test_curl_http_tls_network_size_and_truncation_errors(
    curl_runner, code, status, message, requests
):
    curl_runner.code, curl_runner.status = code, status
    client = e.CurlTransport(Path("synthetic-curl"))
    with pytest.raises(e.EULAError, match=message):
        client.fetch(URL, 65536)
    assert len(curl_runner.calls) - 1 == requests


@pytest.mark.parametrize("size", [0, 65537])
def test_curl_empty_and_oversized_output_guard(curl_runner, size):
    curl_runner.content = b"x" * size
    client = e.CurlTransport(Path("synthetic-curl"))
    with pytest.raises(e.EULAError, match="missing or empty|size limit"):
        client.fetch(URL, 65536)


def test_curl_accepts_exact_limit(curl_runner):
    curl_runner.content = b"x" * 65536
    assert len(e.CurlTransport(Path("synthetic-curl")).fetch(URL, 65536)) == 65536


def test_old_curl_rejected_before_download(curl_runner):
    curl_runner.version = b"curl 8.3.0"
    with pytest.raises(e.EULAError, match="streaming download size limits"):
        e.CurlTransport(Path("synthetic-curl"))
    assert len(curl_runner.calls) == 1


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, subprocess.TimeoutExpired])
def test_dependency_failures_are_sanitized(monkeypatch, failure):
    def run(*_args, **_kwargs):
        if failure is subprocess.TimeoutExpired:
            raise failure("sensitive raw diagnostic", 5)
        raise failure("sensitive raw diagnostic")

    monkeypatch.setattr(e.subprocess, "run", run)
    with pytest.raises(e.EULAError) as error:
        e.CurlTransport(Path("synthetic-curl"))
    assert "sensitive" not in str(error.value)


@pytest.fixture
def local_https(tmp_path, monkeypatch, request):
    """Real curl, disposable CA, loopback routing; production URLs are never contacted."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    executable = shutil.which("curl")
    if os.name == "nt" and (git := shutil.which("git")):
        # Git's OpenSSL curl avoids sandboxed Schannel credential-store failures
        # and exercises the same TLS backend normally used on Debian.
        bundled = Path(git).resolve().parents[1] / "mingw64/bin/curl.exe"
        if bundled.is_file():
            executable = str(bundled)
    if executable is None:
        pytest.skip("curl executable unavailable for loopback HTTPS tests")
    if os.name == "nt" and not (git and bundled.is_file()):
        pytest.skip("Loopback HTTPS tests require OpenSSL curl; sandboxed Schannel is unavailable")
    try:
        client = e.CurlTransport(Path(executable))
    except e.EULAError:
        pytest.skip("Loopback HTTPS tests require curl >= 8.4; bundled OpenSSL curl is too old")
    mode = request.param
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable test CA")])
    now = datetime.now(UTC)
    issuer = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    hostname = "wrong.invalid" if mode == "wrong-host" else "firedash-web.mjblab.de"
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(private_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (
        tmp_path / name
        for name in ("synthetic-ca.pem", "synthetic-server.pem", "synthetic-server.key")
    )
    ca_path.write_bytes(issuer.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = SimpleNamespace(calls=0, exits=[])

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "https://never-contact.invalid/")
            else:
                self.send_response(403 if mode == "waf" else 200)
            self.send_header("Connection", "close")
            if mode == "oversized-stream":
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for _index in range(256):
                        self.wfile.write(b"400\r\n" + b"x" * 1024 + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (OSError, ssl.SSLError):
                    pass  # curl deliberately disconnects at its byte limit.
            else:
                length = (
                    65537 if mode.startswith("oversized") else 64 if mode == "incomplete" else 4
                )
                self.send_header("Content-Length", str(length))
                self.end_headers()
                try:
                    self.wfile.write(b"test")
                except OSError:
                    pass
            self.close_connection = True

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_run = e.subprocess.run

    def run(args, **kwargs):
        if "--url" in args:
            # Test-only routing and CA injection. The production helper has no
            # CA override, redirect allowance or configurable provider origin.
            args = [
                *args,
                "--connect-to",
                f"firedash-web.mjblab.de:443:127.0.0.1:{server.server_port}",
            ]
            if mode != "untrusted-ca":
                args += ["--cacert", str(ca_path)]
        result = real_run(args, **kwargs)
        if "--url" in args:
            state.exits.append(result.returncode)
        return result

    monkeypatch.setattr(e.subprocess, "run", run)
    try:
        yield client, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "local_https",
    [
        "success",
        "oversized-known",
        "oversized-stream",
        "incomplete",
        "redirect",
        "waf",
        "untrusted-ca",
        "wrong-host",
    ],
    indirect=True,
)
def test_real_curl_against_controlled_loopback_https(local_https, request):
    client, state = local_https
    mode = request.node.callspec.params["local_https"]
    if mode == "success":
        assert client.fetch(URL, 65536) == b"test"
    else:
        message = {
            "oversized-known": "size limit",
            "oversized-stream": "size limit",
            "incomplete": "incomplete or truncated",
            "redirect": "redirect",
            "waf": "HTTP 403",
            "untrusted-ca": "TLS certificate",
            "wrong-host": "TLS certificate",
        }[mode]
        with pytest.raises(e.EULAError, match=message):
            client.fetch(URL, 65536)
    assert len(state.exits) == 1
    assert state.calls == (0 if mode in ("untrusted-ca", "wrong-host") else 1)
    if mode.startswith("oversized"):
        assert state.exits == [63]  # curl itself enforced the streaming limit.


MINISIGN = shutil.which("minisign")


@pytest.mark.skipif(
    MINISIGN is None, reason="Minisign executable unavailable; real crypto tests require minisign"
)
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "signature",
        "wrong_key",
        "modified",
        "hash",
        "swapped_documents",
        "swapped_signatures",
        "swapped_pairs",
    ],
)
def test_real_minisign_with_independently_generated_temporary_keys(tmp_path, monkeypatch, change):
    # All secret keys below are new disposable test keys inside pytest scratch.
    # No owner key path, live provider document or production network is used.
    legal = tmp_path / "release-legal"
    legal.mkdir()
    public = legal / "provider-signing.pub"
    secret = tmp_path / "synthetic-test-only.key"
    executable = MINISIGN
    assert executable is not None

    def command(*args):
        result = subprocess.run(
            [executable, *map(str, args)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, "Synthetic Minisign fixture setup failed"

    command("-G", "-W", "-p", public, "-s", secret)
    manifest = copy.deepcopy(json.loads((ROOT / "legal/provider-manifest.json").read_bytes()))
    raw_key = base64.b64decode(public.read_bytes().splitlines()[1])
    manifest["key_id"] = raw_key[2:10][::-1].hex().upper()
    payloads = {}
    for language, record in manifest["languages"].items():
        template = f"Public test template {language}\n".encode()
        provider = f"Independent signed synthetic provider {language}\n".encode()
        (legal / record["template"]).write_bytes(template)
        record["template_sha256"] = e.sha256(template)
        record["provider_sha256"] = e.sha256(provider)
        document = tmp_path / f"synthetic-{language}.md"
        signature = tmp_path / f"synthetic-{language}.minisig"
        document.write_bytes(provider)
        command("-S", "-s", secret, "-m", document, "-x", signature)
        payloads[record["provider_url"]] = provider
        payloads[record["signature_url"]] = signature.read_bytes()
    de, en = (manifest["languages"][language] for language in ("de", "en"))
    if change == "signature":
        payloads[de["signature_url"]] = b"not a signature"
    elif change == "modified":
        payloads[de["provider_url"]] += b"tampered"
    elif change == "wrong_key":
        public.unlink()
        command("-G", "-W", "-p", public, "-s", tmp_path / "second-synthetic-only.key")
        raw_key = base64.b64decode(public.read_bytes().splitlines()[1])
        manifest["key_id"] = raw_key[2:10][::-1].hex().upper()
    elif change == "hash":
        de["provider_sha256"] = "0" * 64
    elif change.startswith("swapped"):
        fields = {
            "swapped_documents": ["provider_url"],
            "swapped_signatures": ["signature_url"],
            "swapped_pairs": ["provider_url", "signature_url"],
        }[change]
        for name in fields:
            payloads[de[name]], payloads[en[name]] = payloads[en[name]], payloads[de[name]]
    (legal / "provider-manifest.json").write_bytes(json.dumps(manifest).encode())
    original = e.tempfile.TemporaryDirectory
    paths = []

    def temporary_directory(**kwargs):
        result = original(dir=tmp_path, **kwargs)
        paths.append(Path(result.name))
        return result

    monkeypatch.setattr(e.tempfile, "TemporaryDirectory", temporary_directory)
    if change == "none":
        result = e.verify_document_set(
            legal, transport=OfflineTransport(payloads), minisign=MINISIGN
        )
        assert [document.language for document in result.documents] == ["de", "en"]
    else:
        with pytest.raises(e.EULAError, match="signature|SHA-256"):
            e.verify_document_set(legal, transport=OfflineTransport(payloads), minisign=MINISIGN)
    assert paths and all(not path.exists() for path in paths)
