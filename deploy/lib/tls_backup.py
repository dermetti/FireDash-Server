"""Root-only, selected-lineage encrypted backup and isolated restore validation.

No application/database credentials, pruning, live restoration or service starts.
Restic transport is deliberately HTTPS REST only: no user-selected commands,
SSH configuration, cloud credential discovery or arbitrary environment injection.
"""

import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import configuration as config
import dns_auth
import tls

RESTIC = "/usr/bin/restic"
RESTORE_ROOT = Path("/var/lib/fire-backend-tls-restore")
TAG = "firedash-tls"


def metadata(path):
    return path.lstat()


def state():
    return tls.ETC / "tls-backup"


def private(path, *, directory=False):
    config.validate_credential_metadata(path, directory=directory)
    for parent in path.parents:
        info = parent.lstat()
        tls.require(
            not parent.is_symlink() and info.st_uid == 0 and info.st_mode & 0o022 == 0,
            "TLS backup parents must be root-controlled",
        )


def repository():
    path = tls.ETC / "tls-backup.conf"
    private(path)
    values = config.read(path)
    tls.require(
        set(values)
        == {
            "RESTIC_REPOSITORY",
            "RESTIC_PASSWORD_FILE",
            "RESTIC_REST_USERNAME",
            "RESTIC_REST_PASSWORD_FILE",
        },
        "TLS backup configuration requires the four documented repository settings",
    )
    url = urlsplit(values["RESTIC_REPOSITORY"].removeprefix("rest:"))
    tls.require(
        values["RESTIC_REPOSITORY"].startswith("rest:https://")
        and url.hostname
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        "TLS backup requires a dedicated HTTPS REST repository without URL credentials",
    )
    tls.require(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", values["RESTIC_REST_USERNAME"])),
        "invalid TLS backup repository identity",
    )
    for key in ("RESTIC_PASSWORD_FILE", "RESTIC_REST_PASSWORD_FILE"):
        private(Path(values[key]))
    # Never place TLS snapshots in the application's pruning domain.
    application = config.read(tls.ETC / "backup.env")
    tls.require(
        application.get("RESTIC_REPOSITORY") != values["RESTIC_REPOSITORY"],
        "TLS backup requires a separate repository from application retention",
    )
    return values


def restic(args):
    values = repository()
    tls.protected_acme_file(Path(RESTIC))
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "RESTIC_REPOSITORY": values["RESTIC_REPOSITORY"],
        "RESTIC_PASSWORD_FILE": values["RESTIC_PASSWORD_FILE"],
        "RESTIC_REST_USERNAME": values["RESTIC_REST_USERNAME"],
        # restic has no REST-password-file setting. Read only here;
        # the root child environment is private to this service.
        "RESTIC_REST_PASSWORD": Path(values["RESTIC_REST_PASSWORD_FILE"]).read_text().strip(),
    }
    result = subprocess.run(  # nosec B603
        [RESTIC, "--no-cache", *args],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    tls.require(
        result.returncode == 0,
        "TLS encrypted backup/restore failed; inspect repository access privately and retry",
    )
    return result.stdout


def provenance(values):
    path = state() / "plugin-provenance.json"
    private(path)
    record = json.loads(path.read_text())
    tls.require(
        isinstance(record, dict)
        and set(record) == {"packages", "source", "plugin_package"}
        and isinstance(record["source"], str)
        and 0 < len(record["source"]) <= 2000,
        "TLS plugin provenance missing or invalid",
    )
    packages = record["packages"]
    tls.require(
        isinstance(packages, dict)
        and 2 <= len(packages) <= 50
        and {"certbot", "acme"}.issubset(packages)
        and all(
            re.fullmatch(r"[a-z][a-z0-9-]{0,79}", k)
            and isinstance(v, str)
            and re.fullmatch(r"[0-9][A-Za-z0-9.+!-]{0,79}", v)
            for k, v in packages.items()
        ),
        "invalid reviewed TLS dependency versions",
    )
    if tls.mode(values) == "letsencrypt-dns01":
        tls.require(record["plugin_package"] in packages, "DNS plugin provenance missing")
        if dns_auth.authenticator(values) == "dns-multi":
            tls.require(
                record["plugin_package"] == "certbot-dns-multi"
                and packages["certbot-dns-multi"] == dns_auth.MULTI_VERSION
                and packages["certbot"] == dns_auth.CERTBOT_VERSION,
                "reviewed dns-multi versions required",
            )
    else:
        tls.require(record["plugin_package"] == "", "unexpected HTTP plugin provenance")
    return record


def check_versions(values, expected):
    binary = Path(tls.certbot_args(values)[0])
    first = binary.read_text().splitlines()[0]
    tls.require(
        bool(re.fullmatch(r"#!/[A-Za-z0-9_./-]+/python[0-9.]*", first)),
        "reviewed Python Certbot entrypoint required for version verification",
    )
    python = Path(first[2:])
    tls.protected_acme_file(python)
    script = (
        "import json,sys; from importlib.metadata import version; "
        "print(json.dumps({p:version(p) for p in json.load(sys.stdin)}))"
    )
    found = json.loads(
        tls.run([str(python), "-I", "-c", script], input=json.dumps(list(expected)).encode())
    )
    tls.require(
        found == expected, "restoration requires the recorded Certbot/plugin dependency versions"
    )


def account_directory(values, params):
    account = params.get("account", "")
    tls.require(
        bool(re.fullmatch(r"[a-f0-9]{32}", account)),
        "selected ACME account identifier missing or invalid",
    )
    server = urlsplit(params["server"])
    tls.require(server.path == "/directory", "unexpected ACME account directory")
    path = tls.acme_root(values) / "accounts" / str(server.hostname) / "directory" / account
    for name in ("meta.json", "regr.json", "private_key.json"):
        private(path / name)
        tls.require(
            isinstance(json.loads((path / name).read_text()), dict), "invalid ACME account metadata"
        )
    return path


def inventory(values):
    """Select only the configured lineage/account, never all of /etc/letsencrypt."""
    tls.require(
        tls.mode(values) != "manual", "automatic TLS backup applies to managed certificates"
    )
    params = tls.check_lineage(values)
    root = tls.acme_root(values)
    selected = [
        tls.ETC / "install.conf",
        root / "renewal" / (tls.cert_name(values) + ".conf"),
        root / "live" / tls.cert_name(values),
        root / "archive" / tls.cert_name(values),
        account_directory(values, params),
        state() / "plugin-provenance.json",
        state() / "dependency-lock.txt",
    ]
    private(state() / "dependency-lock.txt")
    if (tls.ETC / "tls").exists():
        selected.append(tls.ETC / "tls")
    if tls.mode(values) == "letsencrypt-dns01":
        selected.append(
            dns_auth.credentials(values, params, tls.protected_dns_credentials, tls.require)
        )
    records = {}

    def visit(path):
        tls.require(
            path.is_absolute() and not path.is_relative_to(Path("/var/spool")),
            "TLS backup input outside approved scope",
        )
        info = metadata(path)
        tls.require(
            info.st_uid == 0
            and info.st_gid == 0
            and (path.is_symlink() or info.st_mode & 0o022 == 0),
            "TLS backup input must remain root-controlled",
        )
        record = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        if path.is_symlink():
            record.update(kind="link", target=os.readlink(path))
        elif path.is_file():
            record.update(kind="file", sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            if "privkey" in path.name or path.name in {
                "private_key.json",
                "install.conf",
                "dns-recovery.json",
            }:
                private(path)
        else:
            tls.require(path.is_dir(), "unsupported TLS backup file type")
            record.update(kind="directory")
        records[str(path)] = record
        if record["kind"] == "directory":
            for child in sorted(path.iterdir()):
                visit(child)

    for path in selected:
        visit(path)
    for name, entry in records.items():
        if entry["kind"] == "link":
            target = Path(name).parent / entry["target"]
            tls.require(
                not Path(entry["target"]).is_absolute()
                and str(target.resolve(strict=True)) in records,
                "TLS backup symlink escapes selected inventory",
            )
    return records


def manifest(values):
    proof = provenance(values)
    check_versions(values, proof["packages"])
    return {
        "version": 1,
        "hostname": tls.hostname(values["FIREDASH_BASE_URL"]),
        "cert": values["FIREDASH_TLS_CERT_PATH"],
        "key": values["FIREDASH_TLS_KEY_PATH"],
        "provenance": proof,
        "files": inventory(values),
    }


def backup(values):
    repository_config = repository()
    tls.secure_directory(state(), 0o700)
    record = manifest(values)
    path = state() / "manifest.json"
    data = json.dumps(record, sort_keys=True).encode()
    tls.atomic(path, data, 0o600)
    # Exact selected files and symlinks; Restic saves their parent metadata.
    inputs = [name for name, item in record["files"].items() if item["kind"] != "directory"]
    output = restic(["backup", "--json", "--tag", TAG, "--", *sorted(inputs), str(path)])
    summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
    snapshots = [
        item.get("snapshot_id") for item in summaries if item.get("message_type") == "summary"
    ]
    tls.require(
        len(snapshots) == 1 and re.fullmatch(r"[a-f0-9]{64}", snapshots[0] or ""),
        "TLS backup did not confirm an encrypted snapshot",
    )
    tls.atomic(
        state() / "last-success.json",
        json.dumps(
            {
                "time": time.time(),
                "snapshot": snapshots[0],
                "manifest": hashlib.sha256(data).hexdigest(),
                "repository": hashlib.sha256(
                    json.dumps(repository_config, sort_keys=True).encode()
                ).hexdigest(),
            }
        ).encode(),
        0o600,
    )
    print("firedash: selected TLS state encrypted successfully")


def verify(values):
    repository_config = repository()
    path = state() / "last-success.json"
    private(path)
    record = json.loads(path.read_text())
    tls.require(
        record["repository"]
        == hashlib.sha256(json.dumps(repository_config, sort_keys=True).encode()).hexdigest()
        and bool(re.fullmatch(r"[a-f0-9]{64}", record["snapshot"])),
        "TLS backup confirmation belongs to another repository or is invalid",
    )
    tls.require(
        0 <= time.time() - record["time"] <= 48 * 3600,
        "TLS encrypted backup missing or older than 48 hours",
    )
    private(state() / "manifest.json")
    tls.require(
        record["manifest"] == hashlib.sha256((state() / "manifest.json").read_bytes()).hexdigest(),
        "TLS backup confirmation does not match manifest",
    )
    saved = json.loads((state() / "manifest.json").read_text())
    current = manifest(values)
    # Renewal changes bytes. Coverage must include newly created archives and
    # generations; fail until the next successful backup includes them.
    tls.require(
        saved == current, "TLS state changed since encrypted backup; run fire-tls-backup.service"
    )
    for unit in ("fire-tls-backup.service", "fire-tls-backup.timer"):
        tls.verify_owned_file(
            tls.UNITS / unit, (Path(__file__).parent.parent / "systemd" / unit).read_bytes()
        )
        tls.require(
            ".d/" not in tls.run(["systemctl", "cat", unit]).decode(),
            "unreviewed TLS backup unit override",
        )
    tls.run(["systemctl", "is-enabled", "--quiet", "fire-tls-backup.timer"])
    tls.run(["systemctl", "is-active", "--quiet", "fire-tls-backup.timer"])
    print("firedash: selected encrypted TLS backup coverage verified")


def isolated(destination, *, empty=False):
    destination = Path(destination)
    tls.require(
        destination.is_absolute()
        and destination.parent == RESTORE_ROOT
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", destination.name),
        "restore requires a named isolated destination under /var/lib/fire-backend-tls-restore",
    )
    private(RESTORE_ROOT, directory=True)
    private(destination, directory=True)
    if empty:
        tls.require(not any(destination.iterdir()), "restore destination must be empty")
    return destination


def restored_path(destination, original):
    path = Path(original)
    tls.require(path.is_absolute() and ".." not in path.parts, "invalid restored inventory path")
    candidate = destination / path.relative_to(path.anchor)
    tls.require(
        candidate.resolve().is_relative_to(destination.resolve()),
        "restored symlink escapes isolation",
    )
    return candidate


def restore_check(values, destination):
    destination = isolated(destination)
    path = restored_path(destination, str(state() / "manifest.json"))
    private(path)
    record = json.loads(path.read_text())
    tls.require(
        record["version"] == 1 and record["hostname"] == tls.hostname(values["FIREDASH_BASE_URL"]),
        "restored TLS identity differs from reviewed replacement-host configuration",
    )
    files = record["files"]
    tls.require(
        isinstance(files, dict) and 0 < len(files) <= 10000, "invalid restored TLS inventory"
    )
    for name, expected in files.items():
        candidate = restored_path(destination, name)
        info = metadata(candidate)
        tls.require(
            info.st_uid == expected["uid"] == 0
            and info.st_gid == expected["gid"] == 0
            and stat.S_IMODE(info.st_mode) == expected["mode"],
            "restored TLS ownership/permissions differ",
        )
        kind = expected["kind"]
        if kind == "file":
            tls.require(
                stat.S_ISREG(info.st_mode)
                and hashlib.sha256(candidate.read_bytes()).hexdigest() == expected["sha256"],
                "restored TLS file missing or inconsistent",
            )
            if "privkey" in candidate.name or candidate.name in {
                "private_key.json",
                "install.conf",
                "dns-recovery.json",
            }:
                private(candidate)
        elif kind == "link":
            tls.require(
                candidate.is_symlink()
                and os.readlink(candidate) == expected["target"]
                and not Path(expected["target"]).is_absolute()
                and os.path.normpath(Path(name).parent / expected["target"]) in files,
                "restored TLS symlink inconsistent",
            )
        else:
            tls.require(
                kind == "directory" and stat.S_ISDIR(info.st_mode), "invalid restored TLS file type"
            )
    for key in ("cert", "key"):
        tls.require(
            record[key]
            == values["FIREDASH_TLS_CERT_PATH" if key == "cert" else "FIREDASH_TLS_KEY_PATH"],
            "restored certificate paths differ from reviewed configuration",
        )
        target = str(
            Path(Path(record[key]).anchor)
            / restored_path(destination, record[key]).resolve().relative_to(destination.resolve())
        )
        tls.require(
            target in files and files[target]["kind"] == "file",
            "restored certificate missing from inventory",
        )
    # Revalidate semantic requirements independently of recorded hashes/modes.
    installed = config.read(restored_path(destination, str(tls.ETC / "install.conf")))
    for key in (
        "FIREDASH_BASE_URL",
        "FIREDASH_TLS_MODE",
        "FIREDASH_TLS_CERT_PATH",
        "FIREDASH_TLS_KEY_PATH",
        "FIREDASH_TLS_ADOPT_DNS01",
        "FIREDASH_TLS_LINEAGE",
        *dns_auth.KEYS,
    ):
        tls.require(
            installed.get(key) == values.get(key),
            "restored TLS configuration differs from reviewed host",
        )
    renewal = restored_path(
        destination, str(tls.acme_root(values) / "renewal" / (tls.cert_name(values) + ".conf"))
    )
    tls.require(
        str(tls.acme_root(values) / "renewal" / (tls.cert_name(values) + ".conf")) in files,
        "restored renewal metadata missing",
    )
    section = ""
    params = {}
    for line in renewal.read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[renewalparams]" and "=" in line:
            key, value = (item.strip() for item in line.split("=", 1))
            tls.require(key not in params, "duplicate restored renewal parameter")
            params[key] = value
    auth = dns_auth.authenticator(values) if tls.mode(values) == "letsencrypt-dns01" else "webroot"
    tls.require(
        params.get("server") == tls.SERVER
        and params.get("authenticator") == auth
        and params.get("installer", "None") in {"None", "none", ""}
        and not any(
            v
            for k, v in params.items()
            if ("hook" in k and k not in {"renew_hook", "deploy_hook"}) or k.endswith("_command")
        )
        and all(
            params.get(k, "") in {"", f"/usr/bin/python3 {tls.HELPER} hook"}
            for k in ("renew_hook", "deploy_hook")
        ),
        "unsafe restored renewal configuration",
    )
    tls.require(
        bool(re.fullmatch(r"[a-f0-9]{32}", params.get("account", ""))),
        "restored account identifier missing",
    )
    account = (
        tls.acme_root(values)
        / "accounts"
        / str(urlsplit(tls.SERVER).hostname)
        / "directory"
        / params["account"]
    )
    for name in ("meta.json", "regr.json", "private_key.json"):
        original = str(account / name)
        tls.require(original in files, "restored account metadata missing")
        candidate = restored_path(destination, original)
        private(candidate)
        tls.require(
            isinstance(json.loads(candidate.read_text()), dict), "invalid restored account metadata"
        )
    if tls.mode(values) == "letsencrypt-dns01":
        tls.require(
            not any(
                v
                for k, v in params.items()
                if k.endswith("_credentials") and k != dns_auth.credential_parameter(values)
            ),
            "unexpected restored credential mechanism",
        )
        original = str(Path(params.get(dns_auth.credential_parameter(values), "")))
        tls.require(
            original in files
            and (
                not values.get("FIREDASH_TLS_DNS_CREDENTIALS")
                or original == values["FIREDASH_TLS_DNS_CREDENTIALS"]
            ),
            "restored DNS credentials missing",
        )
        candidate = restored_path(destination, original)
        private(candidate)
        if auth == "dns-multi":
            dns_auth.multi_credentials(candidate, values, tls.require)
    else:
        tls.require(
            params.get("webroot_path", "").rstrip(",").strip() == str(tls.WEBROOT),
            "restored webroot differs from reviewed host",
        )
    tls.validate(
        restored_path(destination, record["cert"]),
        restored_path(destination, record["key"]),
        record["hostname"],
    )
    tls.require(
        record["provenance"] == provenance(values),
        "restored plugin provenance differs from reviewed host",
    )
    check_versions(values, record["provenance"]["packages"])
    print("firedash: isolated TLS restore validated; no files activated or services started")


def restore(values, snapshot, destination):
    tls.require(
        bool(re.fullmatch(r"[a-f0-9]{64}", snapshot)),
        "restore requires a full reviewed snapshot ID",
    )
    destination = isolated(destination, empty=True)
    # Require explicit snapshot identity and tag; never implicit latest/subpaths.
    snapshots = json.loads(restic(["snapshots", "--json", "--tag", TAG, snapshot]))
    tls.require(
        len(snapshots) == 1
        and snapshots[0]["id"] == snapshot
        and TAG in snapshots[0].get("tags", []),
        "snapshot is not a selected FireDash TLS backup",
    )
    restic(["restore", snapshot, "--target", str(destination), "--verify"])
    restore_check(values, destination)


def main():
    tls.require(os.geteuid() == 0, "TLS backup requires root")
    private(tls.ETC / "install.conf")
    values = config.read(tls.ETC / "install.conf")
    config.validate_install(values)
    tls.hostname(values["FIREDASH_BASE_URL"])
    with tls.renewal_lock():
        if sys.argv[1:] == ["backup"]:
            backup(values)
        elif sys.argv[1:] == ["verify"]:
            verify(values)
        elif len(sys.argv) == 3 and sys.argv[1] == "restore-check":
            restore_check(values, sys.argv[2])
        elif len(sys.argv) == 4 and sys.argv[1] == "restore":
            restore(values, sys.argv[2], sys.argv[3])
        else:
            raise tls.TLSError(
                "expected backup, verify, restore SNAPSHOT DESTINATION or restore-check DESTINATION"
            )


if __name__ == "__main__":
    try:
        main()
    except tls.TLSError as error:
        # These messages are fixed classifications; no child diagnostics or
        # configuration/JSON exception payloads are included.
        print("firedash error: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Never print exception data from restored JSON, providers or transports.
        print(
            "firedash error: TLS backup/recovery validation failed; "
            "inspect protected configuration, "
            "isolated inventory and repository access; no live state restored",
            file=sys.stderr,
        )
        sys.exit(1)
