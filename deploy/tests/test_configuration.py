"""Focused Installer A tests; no deployment, credentials or production DB access."""

import importlib.util
import os
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "installer_configuration", ROOT / "deploy/lib/configuration.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load installer configuration module")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
bash_path = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
if bash_path is None or not Path(bash_path).is_file():
    raise RuntimeError("Installer configuration tests require Bash")
BASH: str = bash_path


@pytest.fixture
def install():
    return {
        "FIREDASH_INSTALL_SCHEMA_VERSION": "1",
        "FIREDASH_BASE_URL": "https://firedash.test",
        "FIREDASH_TLS_CERT_PATH": "/protected/cert.pem",
        "FIREDASH_TLS_KEY_PATH": "/protected/key.pem",
        "FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY": "http://proxy.test:3128",
        "FIREDASH_REQUESTED_REF": "main",
        "FIREDASH_LAST_SUCCESSFUL_SHA": "a" * 40,
    }


@pytest.fixture
def legacy(install):
    replacements = {
        "$runtime_password": "a" * 64,
        "$secret_key": "test-django-secret",
        "$host": "firedash.test",
        "https://$host": "https://firedash.test",
        "$outbound_mail_https_proxy": install["FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY"],
        "$ingest_upload_bytes": "268435456",
        "$pdf_package_documents": "250",
        "$signing_key_version": "signing-previous",
        "$application_secret_kek_version": "app-2",
        "$report_key_version": "report-2",
    }
    return {k: replacements.get(v, v) for k, v in c.BASELINE.items()}


def write(path, values):
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()), encoding="ascii", newline="\n"
    )


def test_fresh_is_minimal_and_matches_previous_effective_defaults(monkeypatch):
    for key in c.BASELINE:
        monkeypatch.delenv(key, raising=False)
    runtime = c.prepare({}, {}, {}, host="firedash.test", password="fixture", secret="fixture")
    assert set(runtime) == c.MANAGED
    configured = runpy.run_path(str(ROOT / "config/settings/base.py"))
    for key, value in c.BASELINE.items():
        if key in c.MANAGED or key == "MAX_HYDRANT_IMPORT_FEATURES":
            continue
        if value.startswith("$"):
            value = str(c.OVERRIDES[key][2])
        aliases = {
            "DJANGO_STATIC_ROOT": "STATIC_ROOT",
            "ADMIN_SESSION_MAX_AGE_SECONDS": "SESSION_COOKIE_AGE",
        }
        actual = configured[aliases.get(key, key)]
        assert (actual.as_posix() if isinstance(actual, Path) else str(actual)) == value
    assert configured["MAX_HYDRANT_GEOJSON_FEATURES"] == 50000
    assert configured["PUBLICATION_ARTIFACT_MAX_BYTES"] == 629145600
    assert configured["PUBLICATION_BUILD_SUMMARY_MAX_ITEMS"] == 50000
    assert not configured["PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED"]


def test_schema1_preserves_secrets_and_every_selector_idempotently(legacy, install):
    legacy["PUBLICATION_KEK_VERSION"] = "kek-previous"
    runtime = c.prepare(legacy, install, {})
    for key in c.SELECTORS | {
        "POSTGRES_PASSWORD",
        "DJANGO_SECRET_KEY",
        "OUTBOUND_MAIL_HTTPS_PROXY",
    }:
        assert runtime[key] == legacy[key]
    assert set(runtime) == c.MANAGED
    assert c.prepare(runtime, install, {}) == runtime
    assert install["FIREDASH_LAST_SUCCESSFUL_SHA"] == "a" * 40


@pytest.mark.parametrize(
    "key",
    list(c.BASELINE)
    + ["UNKNOWN_LEGACY_SETTING", "MAX_PDF_PACKAGE_BYTES", "MAX_PDF_PACKAGE_MEMBERS"],
)
def test_unclassified_changes_fail_closed_without_values(legacy, install, key):
    if key in c.SELECTORS | {"POSTGRES_PASSWORD", "DJANGO_SECRET_KEY"}:
        return  # Protected dynamic values are deliberately preserved, not baseline-compared.
    legacy[key] = "undisclosed-value"
    with pytest.raises(c.ConfigurationError) as error:
        c.prepare(legacy, install, {})
    assert key in str(error.value)
    assert "undisclosed-value" not in str(error.value)


@pytest.mark.parametrize(
    "text,key",
    [
        (
            "REPORT_TRANSFER_MAX_REPORTS=1\nREPORT_TRANSFER_MAX_REPORTS=2",
            "REPORT_TRANSFER_MAX_REPORTS",
        ),
        ("DJANGO_SECRET_KEY=undisclosed", "DJANGO_SECRET_KEY"),
        (
            "PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=True",
            "PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED",
        ),
        ("MAX_PDF_PACKAGE_DOCUMENTS=$(touch secret)", "MAX_PDF_PACKAGE_DOCUMENTS"),
        ("MAX_PDF_PACKAGE_DOCUMENTS=`whoami`", "MAX_PDF_PACKAGE_DOCUMENTS"),
        ("MAX_PDF_PACKAGE_DOCUMENTS=2;id", "MAX_PDF_PACKAGE_DOCUMENTS"),
        ("MAX_PDF_PACKAGE_DOCUMENTS='2'", "MAX_PDF_PACKAGE_DOCUMENTS"),
        ("export MAX_PDF_PACKAGE_DOCUMENTS=2", "line 1"),
        ("MAX_PDF_PACKAGE_DOCUMENTS", "line 1"),
        ("REPORT_TRANSFER_MAX_REPORTS=101", "REPORT_TRANSFER_MAX_REPORTS"),
        ("REPORT_TRANSFER_MAX_REPORTS=0", "REPORT_TRANSFER_MAX_REPORTS"),
        ("REPORT_TRANSFER_MAX_REPORTS=2.5", "REPORT_TRANSFER_MAX_REPORTS"),
        ("OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS=NaN", "OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS"),
        ("OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS=0", "OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS"),
        ("MAX_INGEST_UPLOAD_BYTES=1", "FILE_LIBRARY_MAX_FILE_BYTES"),
        ("FILE_LIBRARY_MAX_FILE_BYTES=104857601", "FILE_LIBRARY_MAX_FILE_BYTES"),
        (
            "OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS=16",
            "OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS",
        ),
    ],
)
def test_bad_overrides_are_sanitized(text, key):
    with pytest.raises(c.ConfigurationError) as error:
        c.validate_overrides(c.parse(text))
    assert key in str(error.value)
    assert "undisclosed" not in str(error.value)


def test_approved_customization_and_override_removal(legacy, install):
    legacy["MAX_PDF_PACKAGE_DOCUMENTS"] = "200"
    with pytest.raises(c.ConfigurationError, match="MAX_PDF_PACKAGE_DOCUMENTS"):
        c.prepare(legacy, install, {})
    runtime = c.prepare(legacy, install, {"MAX_PDF_PACKAGE_DOCUMENTS": "200"})
    assert runtime["MAX_PDF_PACKAGE_DOCUMENTS"] == "200"
    assert "MAX_PDF_PACKAGE_DOCUMENTS" not in c.prepare(runtime, install, {})
    c.validate_overrides(
        {"REPORT_TRANSFER_MAX_REPORTS": "100", "OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS": "0.5"}
    )


def test_interrupted_configuration_promotion_recovers(tmp_path, legacy, install):
    write(tmp_path / "install.conf", install)
    updated = dict(
        install, FIREDASH_INSTALL_SCHEMA_VERSION="2", FIREDASH_BASE_URL="https://new.test"
    )
    write(tmp_path / "install.conf.pending", updated)
    assert c.choose_install(tmp_path, legacy, {})[0].name == "install.conf"
    runtime = c.prepare(legacy, install, {}, host="new.test")
    selected, recovered = c.choose_install(tmp_path, runtime, {})
    assert selected.name == "install.conf.pending"
    assert recovered["FIREDASH_LAST_SUCCESSFUL_SHA"] == install["FIREDASH_LAST_SUCCESSFUL_SHA"]
    assert c.prepare(runtime, recovered, {}) == runtime


@pytest.mark.parametrize(
    "mode,uid,gid,kind",
    [
        (0o644, 0, 0, stat.S_IFREG),
        (0o640, 1, 0, stat.S_IFREG),
        (0o640, 0, 1, stat.S_IFREG),
        (0o640, 0, 0, stat.S_IFLNK),
        (0o640, 0, 0, stat.S_IFDIR),
    ],
)
def test_invalid_file_metadata_fails_before_read(monkeypatch, mode, uid, gid, kind):
    monkeypatch.setitem(
        sys.modules, "grp", SimpleNamespace(getgrnam=lambda _: SimpleNamespace(gr_gid=2))
    )

    class ProtectedPath:
        name = "install.conf"

        def __truediv__(self, name):
            return self

        def is_symlink(self):
            return False

        def exists(self):
            return True

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0)

        def lstat(self):
            return SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_gid=gid)

    with pytest.raises(c.ConfigurationError, match="install.conf"):
        c.validate_files(ProtectedPath())


def bash(script, *args):
    return subprocess.run([BASH, script, *args], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.parametrize("stage", ["deploy/install.sh", "deploy/install-local.sh"])
def test_output_flags_conflict_before_deployment(stage):
    result = bash(stage, "--verbose", "--quiet")
    # stage 1 needs root only after option parsing; both reject before host mutation.
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


@pytest.mark.parametrize(
    "mode,progress,diagnostic",
    [("default", True, False), ("verbose", True, True), ("quiet", False, False)],
)
def test_output_modes_sanitize_children(mode, progress, diagnostic):
    script = f"""source deploy/lib/common.sh
export FIREDASH_OUTPUT_MODE={mode}
log progress
log_verbose diagnostic
log_warn warning
run_step bash -c '
echo undisclosed-secret
echo undisclosed-secret >&2
echo "firedash error: safe-failure" >&2
exit 7'
rc=$?
log_complete "completed"
exit "$rc"
"""
    result = subprocess.run([BASH, "-c", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 7
    assert ("progress" in result.stdout) == progress
    assert ("diagnostic" in result.stdout) == diagnostic
    assert "warning" in result.stderr and "safe-failure" in result.stderr
    assert "undisclosed-secret" not in result.stdout + result.stderr
    assert "completed" in result.stdout


@pytest.fixture
def installer_harness(tmp_path, legacy, install):
    """Run the real orchestrator with host/service boundaries replaced in a private tree."""
    checkout = tmp_path / "checkout"
    deploy = checkout / "deploy"
    lib = deploy / "lib"
    lib.mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    write(etc / "install.conf", install)
    write(etc / "fire-backend.env", legacy)
    (tmp_path / "cert").touch()
    (tmp_path / "key").touch()
    install = dict(
        install,
        FIREDASH_TLS_CERT_PATH=(tmp_path / "cert").as_posix(),
        FIREDASH_TLS_KEY_PATH=(tmp_path / "key").as_posix(),
    )
    write(etc / "install.conf", install)
    orchestrator = (ROOT / "deploy/install-local.sh").read_text()
    orchestrator = orchestrator.replace(
        "/run/lock/firedash-install.lock", (tmp_path / "lock").as_posix()
    )
    orchestrator = orchestrator.replace("install -d -m 0750 -o root -g root /etc/fire-backend", ":")
    (deploy / "install-local.sh").write_text(orchestrator, newline="\n")
    common = (
        (ROOT / "deploy/lib/common.sh").read_text()
        + """
require_root() { :; }
install_file_atomic() { cp "$1" "$2"; }
flock() { :; }
curl() { :; }
python3() { python "$TEST_CONFIG_DRIVER" "$@"; }
export -f python3
"""
    )
    (lib / "common.sh").write_text(common, newline="\n")
    (lib / "state.sh").write_text("classify_state() { echo ESTABLISHED; }\n")
    stubs = {
        "nginx": "install_nginx() { :; }; validate_tls() { :; }",
        "postgresql": "postgres_bootstrap() { :; }",
        "systemd": "quiesce() { :; }; activate_socket() { :; }; activate_timers() { :; }",
        "admin": "display_admin_setup_url() { :; }",
        "secrets": """validate_outbound_mail_https_proxy() { :; }
validate_premaintenance_secrets() { :; }
effective_outbound_mail_https_proxy() { printf '%s' "$FIREDASH_OUTBOUND_MAIL_HTTPS_PROXY"; }""",
    }
    for name, body in stubs.items():
        (lib / f"{name}.sh").write_text(body + "\n", newline="\n")
    for name in ("bootstrap-lxc", "deploy-release", "verify-deployment"):
        child = deploy / f"{name}.sh"
        child.write_text("#!/usr/bin/env bash\nexit 0\n", newline="\n")
        child.chmod(0o755)
    child = deploy / "initialize-firedash.sh"
    child.write_text(
        """#!/usr/bin/env bash
if [[ $1 == secrets ]]; then
    python3 ignored render > "$FIREDASH_ETC/candidate"
    rc=$?
    (( rc == 0 )) || exit "$rc"
    mv "$FIREDASH_ETC/candidate" "$FIREDASH_ETC/fire-backend.env"
fi
""",
        newline="\n",
    )
    child.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text("""import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location(
    "configuration", os.environ["TEST_CONFIGURATION_MODULE"]
)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load installer configuration module")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
c.validate_files = lambda directory: None  # OS metadata separately tested.
sys.argv = sys.argv[1:]
c.main()
""")
    env = dict(
        os.environ,
        FIREDASH_ETC=etc.as_posix(),
        TEST_CONFIG_DRIVER=driver.as_posix(),
        TEST_CONFIGURATION_MODULE=(ROOT / "deploy/lib/configuration.py").as_posix(),
        FIREDASH_RESOLVED_SHA="b" * 40,
        FIREDASH_REQUESTED_REF="main",
    )

    def run(mode="default", sha="b" * 40):
        env["FIREDASH_RESOLVED_SHA"] = sha
        args = [] if mode == "default" else [f"--{mode}"]
        return subprocess.run(
            [BASH, (deploy / "install-local.sh").as_posix(), *args],
            env=env,
            capture_output=True,
            text=True,
        )

    return run, etc, deploy


def test_orchestrator_completion_same_sha_convergence_and_failed_history(installer_harness):
    run, etc, deploy = installer_harness
    result = run("quiet")
    assert result.returncode == 0, result.stderr
    assert "Resolved SHA: " + "b" * 40 in result.stdout
    assert "successfully deployed SHA: " + "b" * 40 in result.stdout
    assert "phase:" not in result.stdout
    assert c.read(etc / "install.conf")["FIREDASH_LAST_SUCCESSFUL_SHA"] == "b" * 40
    assert c.read(etc / "overrides.env") == {}
    runtime = (etc / "fire-backend.env").read_bytes()
    (etc / "overrides.env").write_text("REPORT_TRANSFER_MAX_REPORTS=100\n")
    result = run("quiet")
    assert result.returncode == 0, result.stderr
    assert "No newer release available" in result.stdout
    assert c.read(etc / "fire-backend.env")["REPORT_TRANSFER_MAX_REPORTS"] == "100"
    (etc / "overrides.env").write_text("")
    result = run("verbose")
    assert result.returncode == 0, result.stderr
    assert (etc / "fire-backend.env").read_bytes() == runtime
    assert "configuration validated" in result.stdout
    (deploy / "verify-deployment.sh").write_text("#!/usr/bin/env bash\nexit 9\n", newline="\n")
    result = run("quiet", sha="c" * 40)
    assert result.returncode != 0
    assert "maintenance/quiesce had begun" in result.stderr
    assert "Rerun this installer to converge" in result.stderr
    assert c.read(etc / "install.conf")["FIREDASH_LAST_SUCCESSFUL_SHA"] == "b" * 40
    for secret in ("test-django-secret", "signing-previous", "http://proxy.test:3128"):
        assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("ref,mode", [(None, "quiet"), ("d" * 40, "verbose"), (None, "default")])
def test_bootstrap_resolves_main_or_exact_sha_and_passes_output_mode(tmp_path, ref, mode):
    bootstrap = (ROOT / "deploy/install.sh").read_text()
    start = bootstrap.index("[[ ${EUID:")
    end = bootstrap.index("# Ensure minimal fetch prerequisites", start)
    # OS/root checks are a boundary; actual argument parsing and Git resolution run.
    bootstrap = bootstrap[:start] + bootstrap[end:]
    script = tmp_path / "install.sh"
    script.write_text(bootstrap, newline="\n")
    trace = tmp_path / "trace"
    runner = r"""
command() {
    if [[ $1 == -v ]]; then return 0; fi
    builtin command "$@"
}
dpkg-query() { echo 'install ok installed'; }
git() {
    printf '%s\n' "$*" >> "$TEST_GIT_TRACE"
    if [[ $* == *rev-parse* ]]; then printf '%s\n' "$TEST_SHA"; return; fi
    if [[ $1 == clone ]]; then checkout=${@: -1};
    elif [[ $1 == init ]]; then checkout=${@: -1};
    else return 0; fi
    mkdir -p "$checkout/deploy"
    cat > "$checkout/deploy/install-local.sh" <<'CHILD'
#!/usr/bin/env bash
printf 'child-mode:%s resolved:%s requested:%s\n' \
    "$FIREDASH_OUTPUT_MODE" "$FIREDASH_RESOLVED_SHA" "$FIREDASH_REQUESTED_REF"
CHILD
}
export -f command dpkg-query git
bash "$TEST_BOOTSTRAP" "$@"
"""
    env = dict(
        os.environ,
        TEST_BOOTSTRAP=script.as_posix(),
        TEST_GIT_TRACE=trace.as_posix(),
        TEST_SHA=ref or "e" * 40,
        FIREDASH_REF="must-not-change-default-main",
    )
    args = [] if mode == "default" else [f"--{mode}"]
    if ref:
        args += ["--ref", ref]
    result = subprocess.run(
        [BASH, "-c", runner, "test", *args], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    commands = trace.read_text()
    if ref:
        assert "fetch -q --depth 1 origin " + ref in commands
    else:
        assert "clone -q --depth 1 --branch main" in commands
    assert (
        f"child-mode:{mode} resolved:{env['TEST_SHA']} requested:{ref or 'main'}" in result.stdout
    )
    if mode == "quiet":
        assert "firedash:" not in result.stdout


@pytest.mark.parametrize(
    "target,entry",
    [
        ("overrides.env", "PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED=True\n"),
        ("fire-backend.env", "MANUAL_SECRET=undisclosed\n"),
        ("install.conf", "UNCLASSIFIED_INSTALL_SETTING=undisclosed\n"),
    ],
)
def test_invalid_configuration_stops_before_maintenance_or_replacement(
    installer_harness, target, entry
):
    run, etc, _ = installer_harness
    with (etc / target).open("a", newline="\n") as stream:
        stream.write(entry)
    before = {name: (etc / name).read_bytes() for name in ("install.conf", "fire-backend.env")}
    result = run()
    assert result.returncode != 0
    assert "phase: quiesce" not in result.stdout
    assert "phase: bootstrap" not in result.stdout
    assert not (etc / "install.conf.pending").exists()
    for name, original in before.items():
        assert (etc / name).read_bytes() == original
    assert "undisclosed" not in result.stdout + result.stderr


def test_related_overrides_validate_as_a_set_and_can_be_removed(legacy, install):
    overrides = {
        "OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS": "100",
        "OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS": "120",
    }
    runtime = c.prepare(legacy, install, overrides)
    assert c.prepare(runtime, install, overrides) == runtime
    assert "OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS" not in c.prepare(runtime, install, {})


def test_child_function_failure_cannot_be_masked_by_a_later_success():
    result = subprocess.run(
        [
            BASH,
            "-c",
            """source deploy/lib/common.sh
phase() { false; echo should-not-run; }
run_step phase
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "should-not-run" not in result.stdout + result.stderr
