"""Release-pinned EULA provider verification; no installer or Django side effects.

Call verify_document_set() from the main thread with the trusted release's legal
directory. Only a complete, immutable in-memory result escapes the private scratch
directory. Transport injection is for controlled tests, not operator configuration.
Requires Debian-packaged curl >= 8.4 and minisign; never discovers signing keys.
"""

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import signal
import stat

# Fixed executables and argument lists; no shell or ambient command configuration.
import subprocess  # nosec B404
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

ORIGIN = "https://firedash-web.mjblab.de"
VERSION = r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,2}(?:-(?:alpha|beta|rc)(?:\.[0-9]{1,6})?)?"
LANGUAGES = ("de", "en")
DOCUMENT_LIMIT = 64 * 1024
SIGNATURE_LIMIT = 8 * 1024
CONNECT_TIMEOUT = 10
REQUEST_TIMEOUT = 30
ATTEMPTS = 2


class EULAError(ValueError):
    """Safe, actionable diagnostic; never includes remote content or raw stderr."""


def require(condition, message):
    if not condition:
        raise EULAError(message)


@dataclass(frozen=True)
class LanguageRecord:
    language: str
    template: str
    template_sha256: str
    provider_url: str
    provider_sha256: str
    signature_url: str


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    agreement_version: str
    agreement_revision: int
    provider_revision: int
    effective_date: str
    status: str
    authoritative_language: str
    key_id: str
    languages: tuple[LanguageRecord, ...]


@dataclass(frozen=True)
class VerifiedDocument:
    language: str
    template: bytes
    provider: bytes
    signature: bytes


@dataclass(frozen=True)
class VerifiedDocumentSet:
    manifest: Manifest
    manifest_sha256: str
    public_key_sha256: str
    documents: tuple[VerifiedDocument, ...]


class Transport(Protocol):
    def fetch(self, url: str, limit: int) -> bytes: ...


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Invalid manifest: duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise EULAError("Invalid manifest: nonstandard JSON number")


def _keys(value, expected):
    require(
        type(value) is dict and set(value) == set(expected),
        "Invalid manifest: missing, unknown or malformed fields",
    )


def _positive_revision(value):
    require(
        type(value) is int and 1 <= value <= 2**31 - 1,
        "Invalid manifest: revision must be a positive integer",
    )


def _download_url(url):
    require(
        type(url) is str
        and re.fullmatch(
            re.escape(ORIGIN) + r"/legal/" + VERSION + r"/PROVIDER\.(?:de|en)\.md(?:\.minisig)?",
            url,
        ),
        "Invalid manifest: HTTPS provider origin or document path is not permitted",
    )


def parse_manifest(content: bytes) -> Manifest:
    """Strict schema 1, with future revisions selected by the trusted release.

    This validates syntax and supported status, not contractual eligibility or
    whether the effective date has arrived. No remote manifest is ever consulted.
    """
    require(len(content) <= 16384, "Invalid manifest: exceeds size limit")
    try:
        data = json.loads(
            content.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, EULAError):
            raise
        raise EULAError("Invalid manifest: malformed UTF-8 JSON") from None
    _keys(
        data,
        (
            "schema_version",
            "agreement_version",
            "agreement_revision",
            "provider_revision",
            "effective_date",
            "status",
            "authoritative_language",
            "key_id",
            "languages",
        ),
    )
    require(
        type(data["schema_version"]) is int and data["schema_version"] == 1,
        "Invalid manifest: unsupported schema version",
    )
    version = data["agreement_version"]
    require(
        type(version) is str and re.fullmatch(VERSION, version),
        "Invalid manifest: unsupported agreement version format",
    )
    _positive_revision(data["agreement_revision"])
    _positive_revision(data["provider_revision"])
    effective = data["effective_date"]
    require(
        type(effective) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective),
        "Invalid manifest: effective date must be YYYY-MM-DD",
    )
    try:
        date.fromisoformat(effective)
    except ValueError:
        raise EULAError("Invalid manifest: effective date is not a calendar date") from None
    require(data["status"] == "approved", "Invalid manifest: unsupported document status")
    require(
        data["authoritative_language"] == "de",
        "Invalid manifest: authoritative language must be German",
    )
    require(
        type(data["key_id"]) is str and re.fullmatch(r"[0-9A-F]{16}", data["key_id"]),
        "Invalid manifest: invalid Minisign key ID",
    )
    _keys(data["languages"], LANGUAGES)
    records = []
    for language in LANGUAGES:
        item = data["languages"][language]
        _keys(
            item,
            ("template", "template_sha256", "provider_url", "provider_sha256", "signature_url"),
        )
        require(
            item["template"] == f"EULA.public.{language}.md",
            "Invalid manifest: unsafe or incorrect template filename",
        )
        for name in ("template_sha256", "provider_sha256"):
            require(
                type(item[name]) is str and re.fullmatch(r"[0-9a-f]{64}", item[name]),
                "Invalid manifest: SHA-256 must be 64 lowercase hexadecimal characters",
            )
        for name, suffix in (("provider_url", ""), ("signature_url", ".minisig")):
            _download_url(item[name])
            require(
                item[name] == f"{ORIGIN}/legal/{version}/PROVIDER.{language}.md{suffix}",
                "Invalid manifest: incorrect language, version or signature URL",
            )
        records.append(LanguageRecord(language=language, **item))
    return Manifest(**{**data, "languages": tuple(records)})


def _read_release_file(path, limit):
    try:
        require(not path.is_symlink(), "Trusted release inputs must not be symlinks")
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(fd, "rb") as stream:
            require(
                stat.S_ISREG(os.fstat(stream.fileno()).st_mode),
                "Trusted release input is not a regular file",
            )
            content = stream.read(limit + 1)
        require(len(content) <= limit, "Trusted release input exceeds size limit")
        return content
    except OSError:
        raise EULAError(
            "Trusted release input missing or unreadable; restore the trusted release"
        ) from None


def _public_key(content, key_id):
    try:
        lines = content.decode("ascii").splitlines()
        require(
            len(lines) == 2 and lines[0].startswith("untrusted comment: "),
            "Incorrect public key: invalid release-local Minisign key format",
        )
        raw = base64.b64decode(lines[1], validate=True)
        require(
            len(raw) == 42 and raw[:2] == b"Ed",
            "Incorrect public key: invalid release-local Minisign key format",
        )
        # Format consistency only. This is never a substitute for Minisign -V.
        require(
            raw[2:10][::-1].hex().upper() == key_id,
            "Incorrect public key: key ID differs from the release manifest",
        )
    except (UnicodeError, binascii.Error):
        raise EULAError("Incorrect public key: invalid release-local Minisign key format") from None


def _text(content, label):
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise EULAError(f"{label}: invalid UTF-8") from None
    require(bool(text.strip()), f"{label}: document is empty")
    require(
        not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\r\t" for c in text),
        f"{label}: unsafe control characters",
    )


def _environment():
    # No ambient proxies, CA overrides, loader hooks, credentials or curl settings.
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if os.name == "nt":
        env.update(
            {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
        )
    return env


def _run(args, *, timeout, stdout=subprocess.PIPE):
    try:
        # Trusted executable and validated literal arguments, never shell input.
        return subprocess.run(  # nosec B603
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=_environment(),
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        raise EULAError(
            "Missing verification dependency; install Debian packages curl and minisign"
        ) from None
    except subprocess.TimeoutExpired:
        raise EULAError(
            "Verification command timed out; check network or dependency health"
        ) from None
    except OSError:
        raise EULAError(
            "Cannot execute verification dependency; check package installation"
        ) from None


class CurlTransport:
    """At most two independent 30-second requests; no curl-internal retries.

    curl >= 8.4 bounds unknown-length transfers during download as well as known
    Content-Length bodies. Each attempt writes only to an anonymous private file.
    """

    def __init__(self, executable=Path("/usr/bin/curl")):
        self.executable = str(executable)
        result = _run([self.executable, "-q", "--version"], timeout=5)
        version = re.match(rb"curl (\d+)\.(\d+)\.(\d+)", result.stdout)
        if result.returncode != 0 or version is None:
            raise EULAError("Cannot identify curl; install Debian-packaged curl >= 8.4")
        require(
            tuple(map(int, version.groups())) >= (8, 4, 0),
            "curl >= 8.4 is required for streaming download size limits",
        )

    def fetch(self, url: str, limit: int) -> bytes:
        _download_url(url)
        require(type(limit) is int and 0 < limit <= DOCUMENT_LIMIT, "Invalid download size limit")
        for attempt in range(ATTEMPTS):
            with (
                tempfile.TemporaryDirectory(prefix="firedash-eula-download-") as directory,
                tempfile.TemporaryFile(mode="w+b", dir=directory) as output,
            ):
                # -q must be first. No redirects, netrc, ambient proxy or TLS bypass.
                result = _run(
                    [
                        self.executable,
                        "-q",
                        "--silent",
                        "--globoff",
                        "--no-netrc",
                        "--proto",
                        "=https",
                        "--proto-redir",
                        "=https",
                        "--no-location",
                        "--max-redirs",
                        "0",
                        "--no-insecure",
                        "--proxy",
                        "",
                        "--connect-timeout",
                        str(CONNECT_TIMEOUT),
                        "--max-time",
                        str(REQUEST_TIMEOUT),
                        "--retry",
                        "0",
                        "--max-filesize",
                        str(limit),
                        "--fail",
                        "--output",
                        "-",
                        "--write-out",
                        "%{stderr}%{http_code}",
                        "--url",
                        url,
                    ],
                    timeout=REQUEST_TIMEOUT + 5,
                    stdout=output,
                )
                status = result.stderr
                if status and re.fullmatch(rb"[0-9]{3}", status) and status != b"000":
                    code = int(status)
                    require(
                        not 300 <= code < 400,
                        "Provider retrieval rejected redirect; direct HTTPS 200 required",
                    )
                    if code in (404, 410):
                        raise EULAError(
                            "Provider document or detached signature missing (HTTP 404/410)"
                        )
                    require(
                        code == 200,
                        f"Provider retrieval HTTP {code}; "
                        "check provider availability or WAF rejection",
                    )
                if result.returncode in {5, 6, 7, 28, 52, 55, 56} and attempt + 1 < ATTEMPTS:
                    continue
                if result.returncode in {35, 51, 58, 60, 77, 80, 82, 83, 90, 91}:
                    raise EULAError(
                        "Provider TLS certificate validation failed; "
                        "check certificate and system trust store"
                    )
                if result.returncode == 28:
                    raise EULAError("Provider network timeout; check connectivity and retry")
                if result.returncode == 63:
                    raise EULAError("Provider download exceeds size limit")
                if result.returncode in {18, 52, 56}:
                    raise EULAError(
                        "Provider download incomplete or truncated; retry with a complete response"
                    )
                require(
                    result.returncode == 0,
                    "Provider network retrieval failed; check HTTPS connectivity",
                )
                require(status == b"200", "Provider retrieval did not confirm HTTP 200")
                output.seek(0)
                content = output.read(limit + 1)
                require(len(content) <= limit, "Provider download exceeds size limit")
                require(bool(content), "Provider document or detached signature missing or empty")
                return content
        raise EULAError("Provider retrieval attempts exhausted")  # pragma: no cover


@contextlib.contextmanager
def _termination_cleanup():
    require(
        threading.current_thread() is threading.main_thread(),
        "EULA verification must run on the main thread for termination-safe cleanup",
    )
    previous = {}

    def interrupted(_signum, _frame):
        raise EULAError("EULA verification interrupted; temporary documents discarded")

    try:
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous[signum] = signal.signal(signum, interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _scratch_file(directory, content):
    fd, filename = tempfile.mkstemp(dir=directory)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    return Path(filename)


def verify_document_set(
    legal_dir: Path, *, transport: Transport | None = None, minisign=Path("/usr/bin/minisign")
) -> VerifiedDocumentSet:
    """Return both verified languages or raise EULAError, never a partial result.

    legal_dir is part of the caller's trusted software release, not a downloaded
    bundle or user-selected manifest. Returned bytes are original and immutable.
    SIGTERM/SIGHUP handlers are restored on exit; SIGINT also unwinds cleanup.
    No service, database, persistent cache or installation state is touched.
    """
    legal_dir = Path(legal_dir)
    require(not legal_dir.is_symlink(), "Trusted legal directory must not be a symlink")
    raw_manifest = _read_release_file(legal_dir / "provider-manifest.json", 16384)
    manifest = parse_manifest(raw_manifest)
    key = _read_release_file(legal_dir / "provider-signing.pub", 1024)
    _public_key(key, manifest.key_id)
    templates = {}
    for record in manifest.languages:
        template = _read_release_file(legal_dir / record.template, 256 * 1024)
        require(
            sha256(template) == record.template_sha256,
            f"Public template integrity failure ({record.language}); restore the trusted release",
        )
        _text(template, f"Public template ({record.language})")
        templates[record.language] = template

    documents = []
    with _termination_cleanup(), tempfile.TemporaryDirectory(prefix="firedash-eula-") as directory:
        # TemporaryDirectory/mkstemp use 0700/0600 on POSIX, independent of umask.
        if os.name == "posix":
            os.chmod(directory, 0o700)
        public_key = _scratch_file(directory, key)
        dependency = _run([str(minisign), "-v"], timeout=5)
        require(
            dependency.returncode == 0,
            "Cannot identify Minisign; install the Debian minisign package",
        )
        client = transport if transport is not None else CurlTransport()
        for record in manifest.languages:
            provider = client.fetch(record.provider_url, DOCUMENT_LIMIT)
            signature = client.fetch(record.signature_url, SIGNATURE_LIMIT)
            require(
                type(provider) is bytes and 0 < len(provider) <= DOCUMENT_LIMIT,
                f"Provider document ({record.language}) missing or oversized",
            )
            require(
                type(signature) is bytes and 0 < len(signature) <= SIGNATURE_LIMIT,
                f"Detached signature ({record.language}) missing or oversized",
            )
            document_path = _scratch_file(directory, provider)
            signature_path = _scratch_file(directory, signature)
            result = _run(
                [
                    str(minisign),
                    "-V",
                    "-q",
                    "-p",
                    str(public_key),
                    "-m",
                    str(document_path),
                    "-x",
                    str(signature_path),
                ],
                timeout=15,
            )
            require(
                result.returncode == 0,
                f"Invalid signature ({record.language}); "
                "check detached signature, language and release public key",
            )
            require(
                sha256(provider) == record.provider_sha256,
                f"Provider SHA-256 mismatch ({record.language}); "
                "incorrect, modified or swapped language document",
            )
            _text(provider, f"Provider document ({record.language})")
            documents.append(
                VerifiedDocument(record.language, templates[record.language], provider, signature)
            )
    return VerifiedDocumentSet(manifest, sha256(raw_manifest), sha256(key), tuple(documents))
