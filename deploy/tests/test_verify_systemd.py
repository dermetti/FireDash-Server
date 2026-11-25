"""Disposable manager-response fixtures; not native systemd acceptance."""

import importlib.util
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "verify_systemd", ROOT / "deploy/lib/verify_systemd.py"
)
assert spec and spec.loader
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def manager(monkeypatch):
    overrides: dict[str, str | Exception | None] = {}

    def configuration(unit):
        filename = unit.split("@")[0] + "@.service" if "@" in unit else unit
        entries: dict[str, list[str]] = {}
        for line in (ROOT / "deploy/systemd" / filename).read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                entries.setdefault(key, []).append(value)
        return entries

    def command(args):
        if args[:2] == ["systemctl", "cat"]:
            assert args[2] == "--"
            unit = args[3]
            filename = unit.split("@")[0] + "@.service" if "@" in unit else unit
            text = (ROOT / "deploy/systemd" / filename).read_text()
            if "EnvironmentFiles" in overrides:
                text += f"\n[Service]\nEnvironmentFile={overrides['EnvironmentFiles']}\n"
            for name in (
                "LoadCredential",
                "LoadCredentialEncrypted",
                "SetCredential",
                "SetCredentialEncrypted",
                "ImportCredential",
            ):
                if name in overrides:
                    override = overrides[name]
                    assert isinstance(override, str)
                    fields = shlex.split(override)
                    value = ":".join(fields[2:])
                    text += f"\n[Service]\n{name}={value}\n"
            return text
        if args[0] == "systemctl":
            assert args[1:3] == ["show", "--all"], "base-unit text must not establish safety"
            name, unit = args[3].split("=", 1)[1], args[4]
            if name in overrides:
                overridden = overrides[name]
                if overridden is None:
                    return ""
                if isinstance(overridden, Exception):
                    raise overridden
                return f"{name}={overridden}"
            entries = configuration(unit)
            value = entries.get(name, [""])[-1]
            value = {"true": "yes", "512M": "536870912"}.get(value, value)
            value = {
                "LoadState": "loaded",
                "NeedDaemonReload": "no",
                "CPUQuotaPerSecUSec": "1s",
                "TimeoutStartUSec": "1min",
            }.get(name, value)
            return f"{name}={value}"
        raise AssertionError(args)

    monkeypatch.setattr(verifier, "command", command)
    return overrides


def test_expected_effective_properties(manager):
    verifier.verify()


@pytest.mark.parametrize(
    "name,value",
    [
        ("User", "root"),
        ("Group", "root"),
        ("NoNewPrivileges", "no"),
        ("PrivateNetwork", "no"),
        ("MemoryMax", "infinity"),
        ("TasksMax", "infinity"),
        ("CPUQuotaPerSecUSec", "infinity"),
        ("TimeoutStartUSec", "infinity"),
        ("EnvironmentFiles", "/etc/fire-backend/fire-backend.env"),
        ("Environment", "SECRET=disposable"),
        ("SocketUser", "nobody"),
        ("SocketGroup", "nogroup"),
        ("SocketMode", "0666"),
        ("LoadState", "not-found"),
        ("NeedDaemonReload", "yes"),
        ("Accept", "no"),
        ("StandardInput", "null"),
        ("StandardOutput", "journal"),
        ("StandardError", "null"),
        ("LoadCredential", "a(ss) 0"),
        ("LoadCredentialEncrypted", 'a(ss) 1 "secret" "/wrong"'),
        ("SetCredential", 'a(say) 1 "secret" 1 42'),
        ("ImportCredential", 'as 1 "*"'),
    ],
)
def test_effective_override_rejected_despite_matching_base_units(manager, name, value):
    manager[name] = value
    with pytest.raises(verifier.VerificationError):
        verifier.verify()


@pytest.mark.parametrize(
    "name",
    [
        "User",
        "NoNewPrivileges",
        "PrivateNetwork",
        "MemoryMax",
        "TasksMax",
        "CPUQuotaPerSecUSec",
        "TimeoutStartUSec",
        "SocketMode",
        "Environment",
    ],
)
@pytest.mark.parametrize(
    "missing", [None, "[unprintable]", "", verifier.VerificationError("inspection failed")]
)
def test_essential_missing_or_uninspectable_properties_fail(manager, name, missing):
    manager[name] = missing
    # Empty Environment is the required worker policy; a missing property
    # (no name= prefix) is distinct and must fail.
    if name == "Environment" and missing == "":
        verifier.verify()
    else:
        with pytest.raises(verifier.VerificationError):
            verifier.verify()


def test_native_checks_remain_wired():
    script = (ROOT / "deploy/verify-deployment.sh").read_text()
    assert 'python3 "$SELF_DIR/lib/verify_systemd.py"' in script
    assert "verify_library_pdf_sanitizer" in script
    assert "root:fire_backend:660" in script
    assert "fire_backend:www-data:660" in script
    assert '"$SECRET_DIR/$f") == "root:root:600"' in script


@pytest.mark.parametrize(
    "text,allowed",
    [
        (
            "[Service]\nLoadCredential=old:/old\n[Service]\nLoadCredential=\nLoadCredential=expected:/expected\n",
            True,
        ),
        ("[Service]\nLoadCredential=expected:/expected\n[Service]\nLoadCredential=\n", False),
        ("[Service]\nLoadCredential=expected:/expected\nLoadCredential=extra:/extra\n", False),
        ("[Service]\nLoadCredential=expected:/wrong\n", False),
        (
            "[Service]\nLoadCredential=expected:/expected\nSetCredentialEncrypted=secret:disposable\n",
            False,
        ),
        ("", False),
        ("[unprintable]", False),
        ("[Service]\nLoadCredential=expected:/expected\nUnfinished=\\", False),
    ],
)
def test_credential_dropin_resets_and_conflicts(monkeypatch, text, allowed):
    def command(args):
        if args[1] == "show":
            return "NeedDaemonReload=no"
        assert args[1:3] == ["cat", "--"]
        return text

    monkeypatch.setattr(verifier, "command", command)
    if allowed:
        verifier.credentials("disposable.service", [("expected", "/expected")])
    else:
        with pytest.raises(verifier.VerificationError):
            verifier.credentials("disposable.service", [("expected", "/expected")])


@pytest.mark.parametrize("reset,allowed", [(False, False), (True, True)])
def test_empty_environment_files_uses_ordered_declarations(monkeypatch, reset, allowed):
    text = "[Service]\nEnvironmentFile=/disposable/application.env\n"
    if reset:
        text += "[Service]\nEnvironmentFile=\n"

    def command(args):
        if args[1] == "show":
            assert args[3] == "--property=NeedDaemonReload"
            return "NeedDaemonReload=no"
        return text

    monkeypatch.setattr(verifier, "command", command)
    if allowed:
        verifier.credentials("disposable.service", [], no_environment=True)
    else:
        with pytest.raises(verifier.VerificationError):
            verifier.credentials("disposable.service", [], no_environment=True)
