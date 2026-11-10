"""Literal installer configuration, narrow schema-1 migration, and runtime render.

No configuration is executed. Diagnostics contain names only, never values.
The frozen baseline is deployment history, not application defaults.
"""

import json
import os
import re
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

BASELINE = json.loads(Path(__file__).with_name("legacy-schema-1.json").read_text())
SELECTORS = {
    "PUBLICATION_KEK_VERSION",
    "PUBLICATION_SIGNING_KEY_VERSION",
    "APPLICATION_SECRET_KEK_VERSION",
    "REPORT_TRANSFER_ACTIVE_KEY",
}
MANAGED = {
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "FIREDASH_PUBLIC_ORIGIN",
    "REFERENCE_DATA_ACCEPTED_GROUP",
    "TRUSTED_PROXY_IPS",
    "OUTBOUND_MAIL_HTTPS_PROXY",
    "REPORT_TRANSFER_ROOT",
} | SELECTORS
# Upper bounds keep tuning inside the existing upload/expansion/sandbox envelope.
OVERRIDES = {
    "MAX_INGEST_UPLOAD_BYTES": (1, 536870912, 268435456),
    "MAX_PDF_PACKAGE_DOCUMENTS": (1, 1000, 250),
    "FILE_LIBRARY_MAX_FILE_BYTES": (1, 104857600, 26214400),
    "REPORT_TRANSFER_MAX_REPORTS": (1, 100, 20),
    "OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS": (Decimal("0.1"), 120, 5),
    "OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS": (Decimal("0.1"), 300, 15),
    "OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS": (Decimal("0.1"), 300, 15),
}
INSTALL_KEYS = {
    "FIREDASH_INSTALL_SCHEMA_VERSION",
    "FIREDASH_BASE_URL",
    "FIREDASH_TLS_CERT_PATH",
    "FIREDASH_TLS_KEY_PATH",
    "FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY",
    "FIREDASH_REQUESTED_REF",
    "FIREDASH_LAST_SUCCESSFUL_SHA",
}
KEY = re.compile(r"[A-Z][A-Z0-9_]*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ConfigurationError(ValueError):
    pass


def fail(key, reason):
    raise ConfigurationError(f"{key}: {reason}")


def parse(text):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not KEY.fullmatch(key):
            fail(f"line {number}", "expected literal KEY=VALUE")
        if key in result:
            fail(key, "duplicate setting")
        # No quoting, expansion, shell operators, inline comments or whitespace.
        if any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in value) or any(
            c in value for c in "\"'`$;|&<>\\#(){}"
        ):
            fail(key, "expected literal value; shell syntax is forbidden")
        result[key] = value
    return result


def read(path):
    return parse(path.read_text(encoding="ascii")) if path.exists() else {}


def validate_overrides(values):
    for key, value in values.items():
        if key not in OVERRIDES:
            fail(key, "unknown or forbidden override; remove from overrides.env")
        minimum, maximum, _ = OVERRIDES[key]
        pattern = r"[0-9]+(?:\.[0-9]+)?" if "TIMEOUT" in key else r"[0-9]+"
        if not re.fullmatch(pattern, value):
            fail(key, "invalid numeric type")
        try:
            numeric = Decimal(value)
        except InvalidOperation:
            fail(key, "invalid numeric type")
        if not minimum <= numeric <= maximum:
            fail(key, "outside approved numeric range")
    effective = {k: Decimal(values.get(k, str(v[2]))) for k, v in OVERRIDES.items()}
    if effective["FILE_LIBRARY_MAX_FILE_BYTES"] > effective["MAX_INGEST_UPLOAD_BYTES"]:
        fail("FILE_LIBRARY_MAX_FILE_BYTES", "must not exceed MAX_INGEST_UPLOAD_BYTES")
    if (
        effective["OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS"]
        > effective["OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS"]
    ):
        fail("OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS", "must not exceed read timeout")


def validate_files(directory):
    import grp

    if directory.is_symlink():
        fail("configuration directory", "symlinks are forbidden")
    if directory.exists():
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail("configuration directory", "requires root ownership and no group/other writes")
    for name in ("install.conf", "install.conf.pending", "overrides.env", "fire-backend.env"):
        path = directory / name
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            fail(name, "requires a regular root-owned file")
        expected_group = grp.getgrnam("fire_backend").gr_gid if name == "fire-backend.env" else 0
        if info.st_gid != expected_group or stat.S_IMODE(info.st_mode) != 0o640:
            fail(name, "requires configured owner/group and mode 0640")
    credentials = directory / "credentials"
    if credentials.exists() or credentials.is_symlink():
        info = credentials.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            fail("credentials", "requires a real root:root 0700 directory")
        for path in credentials.iterdir():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                fail("credentials", "requires regular root:root 0600 files")


def validate_install(values):
    for key in values:
        if key not in INSTALL_KEYS:
            fail(key, "unknown install.conf setting; review before retrying")
    if values and values.get("FIREDASH_INSTALL_SCHEMA_VERSION") not in {"1", "2"}:
        fail("FIREDASH_INSTALL_SCHEMA_VERSION", "unsupported schema")
    if values:
        for key in (
            "FIREDASH_BASE_URL",
            "FIREDASH_TLS_CERT_PATH",
            "FIREDASH_TLS_KEY_PATH",
            "FIREDASH_REQUESTED_REF",
        ):
            if not values.get(key):
                fail(key, "missing installation identity")
    for key in ("FIREDASH_BASE_URL",):
        if key in values:
            url = urlsplit(values[key])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.netloc != url.hostname
                or "." not in url.hostname
                or url.path
                or url.query
                or url.fragment
            ):
                fail(key, "requires HTTPS public origin")
    if "FIREDASH_LAST_SUCCESSFUL_SHA" in values and not re.fullmatch(
        r"[0-9a-f]{40}", values["FIREDASH_LAST_SUCCESSFUL_SHA"]
    ):
        fail("FIREDASH_LAST_SUCCESSFUL_SHA", "invalid release history")


def prepare(old, install, overrides, *, host=None, proxy=None, password="", secret=""):
    validate_install(install)
    validate_overrides(overrides)
    previous_host = urlsplit(install.get("FIREDASH_BASE_URL", "")).hostname
    if old and not previous_host:
        fail("FIREDASH_BASE_URL", "existing runtime requires persisted installation identity")
    host = host or previous_host
    if not host:
        fail("FIREDASH_BASE_URL", "hostname unavailable")
    old_proxy = install.get(
        "FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY", old.get("OUTBOUND_MAIL_HTTPS_PROXY", "")
    )
    if proxy is None:
        proxy = old_proxy
    variables = {
        "$host": previous_host or host,
        "https://$host": "https://" + (previous_host or host),
        "$outbound_mail_https_proxy": old_proxy,
        "$ingest_upload_bytes": "268435456",
        "$pdf_package_documents": "250",
    }
    legacy = "MAX_PDF_INPUT_BYTES" in old
    expected_keys = set(BASELINE) if legacy else MANAGED
    if old:
        for key in expected_keys - old.keys():
            fail(key, "missing managed setting; restore configuration before retrying")
    validate_overrides({key: value for key, value in old.items() if key in OVERRIDES})
    for key, value in old.items():
        if key in SELECTORS:
            pattern = r"[A-Za-z0-9._-]{1,64}" if key == "REPORT_TRANSFER_ACTIVE_KEY" else VERSION
            if not re.fullmatch(pattern, value):
                fail(key, "invalid protected selector")
        elif key in {"POSTGRES_PASSWORD", "DJANGO_SECRET_KEY"}:
            if not value:
                fail(key, "missing managed secret")
        elif key in OVERRIDES:
            default = str(OVERRIDES[key][2])
            if legacy and value != default and overrides.get(key) != value:
                fail(
                    key, "custom runtime value requires explicit matching approval in overrides.env"
                )
        elif key not in BASELINE or (not legacy and key not in MANAGED):
            fail(key, "unknown runtime setting; review before retrying")
        elif value != variables.get(BASELINE[key], BASELINE[key]):
            fail(key, "differs from installer baseline; review customization before retrying")
    result = {k: v for k, v in BASELINE.items() if k in MANAGED}
    result.update(
        {
            "POSTGRES_PASSWORD": password or old.get("POSTGRES_PASSWORD", ""),
            "DJANGO_SECRET_KEY": secret or old.get("DJANGO_SECRET_KEY", ""),
            "DJANGO_ALLOWED_HOSTS": host,
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://" + host,
            "FIREDASH_PUBLIC_ORIGIN": "https://" + host,
            "OUTBOUND_MAIL_HTTPS_PROXY": proxy,
        }
    )
    for key in SELECTORS:
        result[key] = old.get(key, "1")
    result.update(overrides)
    # Re-parse the complete candidate to validate CLI/environment inputs as data too.
    parse("\n".join(f"{k}={v}" for k, v in result.items()))
    return result


def choose_install(directory, old, overrides):
    """Recover either side of an interrupted two-file configuration promotion."""
    current = directory / "install.conf"
    pending = directory / "install.conf.pending"
    errors = []
    for path in (current, pending):
        if not path.exists():
            continue
        values = read(path)
        try:
            validate_install(values)
            if old:
                prepare(old, values, overrides)
            return path, values
        except ConfigurationError as error:
            errors.append(error)
    if errors:
        raise errors[0]
    if old:
        fail("install.conf", "missing persisted installation identity")
    return current, {}


def main():
    directory = Path(os.environ.get("FIREDASH_ETC", "/etc/fire-backend"))
    validate_files(directory)
    old = read(directory / "fire-backend.env")
    overrides = read(directory / "overrides.env")
    # Parse both files even when only one is used for interrupted-run recovery.
    for name in ("install.conf", "install.conf.pending"):
        validate_install(read(directory / name))
    validate_overrides(overrides)
    install_path, install = choose_install(directory, old, overrides)
    command = sys.argv[1]
    if command == "install-source":
        print(install_path.as_posix())
        return
    if command == "check-files":
        if old:
            prepare(old, install, overrides)
        return
    for key in INSTALL_KEYS:
        if key in os.environ:
            parse(f"{key}={os.environ[key]}")
    candidate_install = dict(install)
    for key in (
        "FIREDASH_BASE_URL",
        "FIREDASH_TLS_CERT_PATH",
        "FIREDASH_TLS_KEY_PATH",
        "FIREDASH_REQUESTED_REF",
    ):
        if key in os.environ:
            candidate_install[key] = os.environ[key]
    if candidate_install:
        candidate_install["FIREDASH_INSTALL_SCHEMA_VERSION"] = "2"
        validate_install(candidate_install)
    proxy = os.environ.get("FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY")
    if proxy:
        url = urlsplit(proxy)
        if url.scheme not in {"http", "https"} or not url.hostname:
            fail("FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY", "invalid proxy URL")
    result = prepare(
        old,
        install,
        overrides,
        host=os.environ.get("FIREDASH_HOST"),
        proxy=os.environ.get("FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY"),
        password=os.environ.get("FIREDASH_RUNTIME_PASSWORD", ""),
        secret=os.environ.get("FIREDASH_DJANGO_SECRET", ""),
    )
    if command == "render":
        if not result["POSTGRES_PASSWORD"] or not result["DJANGO_SECRET_KEY"]:
            fail("managed secrets", "unavailable")
        sys.stdout.write("".join(f"{k}={v}\n" for k, v in result.items()))
    elif command != "prepare":
        fail("configuration command", "unsupported")


if __name__ == "__main__":
    try:
        main()
    except (ConfigurationError, OSError, UnicodeError, KeyError, ValueError):
        error = sys.exc_info()[1]
        message = (
            str(error)
            if isinstance(error, ConfigurationError)
            else "configuration: cannot validate protected files"
        )
        print("firedash error: " + message, file=sys.stderr)
        sys.exit(1)
