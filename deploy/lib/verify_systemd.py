"""Read effective systemd boundaries; never print credential values or child output."""

import shlex
import subprocess  # nosec B404


class VerificationError(ValueError):
    pass


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)  # nosec B603
    if result.returncode:
        raise VerificationError("systemd property inspection failed")
    return result.stdout.strip()


def property_value(unit, name):
    output = command(["systemctl", "show", "--all", f"--property={name}", unit])
    prefix = name + "="
    if not output.startswith(prefix) or "\n" in output:
        raise VerificationError(f"{unit}: cannot inspect {name}")
    return output[len(prefix) :]


def expect(unit, name, value):
    if property_value(unit, name) != value:
        raise VerificationError(f"{unit}: unsafe effective {name}")


def credentials(unit, expected, *, no_environment=False):
    # Debian 13/systemd 257 cannot render structured credentials through
    # systemctl show. Check ordered fragments/drop-ins with list-reset semantics,
    # after requiring the manager has no pending reload. Never print this text.
    expect(unit, "NeedDaemonReload", "no")
    text = command(["systemctl", "cat", "--", unit])
    if not text:
        raise VerificationError(f"{unit}: cannot inspect credential configuration")
    names = {
        "LoadCredential",
        "LoadCredentialEncrypted",
        "SetCredential",
        "SetCredentialEncrypted",
        "ImportCredential",
    }
    if no_environment:
        # systemd 257 emits no property line for an empty EnvironmentFiles
        # array, even with --all. Inspect its ordered declarations here too.
        names.add("EnvironmentFile")
    values: dict[str, list[str]] = {}
    section = pending = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service" or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in names:
            continue
        if not value:
            values[name] = []
        else:
            if "\\" in value or "%" in value:
                raise VerificationError(f"{unit}: ambiguous credential configuration")
            values.setdefault(name, []).extend(shlex.split(value))
    if pending or not section:
        raise VerificationError(f"{unit}: incomplete credential configuration")
    loads = values.get("LoadCredential", [])
    if sorted(loads) != sorted(f"{name}:{path}" for name, path in expected) or any(
        values.get(name) for name in names - {"LoadCredential"}
    ):
        raise VerificationError(f"{unit}: unsafe credential separation in fragments/drop-ins")
    expect(unit, "NeedDaemonReload", "no")


def verify():
    instance = "00000000-0000-4000-8000-000000000000"
    worker = f"fire-pdf-sanitizer@{instance}.service"
    broker = f"fire-pdf-sanitizer-broker@{instance}.service"
    identities = {
        "fire-backend.service": "fire_backend",
        "fire-publication-build.service": "fire_publication",
        "fire-publication-delivery.service": "fire_publication",
        "fire-publication-maintenance.service": "fire_publication",
        "fire-import-staging-maintenance.service": "fire_backend",
        worker: "fire_pdf_sanitizer",
        broker: "root",
    }
    public = (
        "publication-signing-public-key-ring",
        "/etc/fire-backend/credentials/publication-signing-public-key-ring.json",
    )
    private = [
        ("publication-kek", "/etc/fire-backend/credentials/publication-kek"),
        ("publication-signing-key", "/etc/fire-backend/credentials/publication-signing-key"),
        public,
    ]
    for unit, identity in identities.items():
        expect(unit, "LoadState", "loaded")
        expect(unit, "NeedDaemonReload", "no")
        expect(unit, "User", identity)
        expect(unit, "Group", identity)
        expect(unit, "NoNewPrivileges", "yes")
        expected = []
        if unit == "fire-backend.service":
            expected = [
                public,
                (
                    "application-secret-kek-ring",
                    "/etc/fire-backend/credentials/application-secret-kek-ring.json",
                ),
                (
                    "report-transfer-keyring",
                    "/etc/fire-backend/credentials/report-transfer-keyring.json",
                ),
            ]
        elif unit in ("fire-publication-build.service", "fire-publication-delivery.service"):
            expected = private
        credentials(unit, expected, no_environment=unit in (worker, broker))
    for unit in (worker, broker):
        expect(unit, "PrivateNetwork", "yes")
        expect(unit, "Environment", "")
    for name, value in {
        "MemoryMax": "536870912",
        "TasksMax": "32",
        "CPUQuotaPerSecUSec": "1s",
        "TimeoutStartUSec": "1min",
    }.items():
        expect(worker, name, value)
    for unit, user, group in (
        ("fire-backend.socket", "fire_backend", "www-data"),
        ("fire-pdf-sanitizer-broker.socket", "", "fire_backend"),
        ("fire-publication-build.socket", "", "fire_backend"),
    ):
        expect(unit, "LoadState", "loaded")
        expect(unit, "NeedDaemonReload", "no")
        expect(unit, "SocketUser", user)
        expect(unit, "SocketGroup", group)
        expect(unit, "SocketMode", "0660")
    expect("fire-pdf-sanitizer-broker.socket", "Accept", "yes")
    expect(broker, "StandardInput", "socket")
    expect(broker, "StandardOutput", "inherit")
    expect(broker, "StandardError", "journal")


if __name__ == "__main__":
    try:
        verify()
    except (VerificationError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        # Only our own static messages are safe; parser/OS failures may quote data.
        print(str(error) if isinstance(error, VerificationError) else "systemd inspection failed")
        raise SystemExit(1) from None
