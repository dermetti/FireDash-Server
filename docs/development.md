# Development

## Local environment

Use Python 3.13 or the repository-supported interpreter, PostgreSQL with
PostGIS, and a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements\dev.txt
```

On a Unix host, use `source venv/bin/activate` instead. Development settings
are selected by `DJANGO_SETTINGS_MODULE=config.settings.development`; set a
development-only `DJANGO_SECRET_KEY` when required. Do not use production
credential files locally.

Create a PostGIS-enabled development database, apply migrations, then run:

```powershell
python manage.py migrate
python manage.py runserver
pytest -p no:cacheprovider
```

The normal test settings need a PostgreSQL role that may create a test
database. Do not change production HBA rules or production roles to satisfy a
local test failure.

## Quality checks

Run the checks relevant to your change before opening review:

```powershell
ruff format --check .
ruff check .
mypy .
python manage.py check
python manage.py makemigrations --check --dry-run
pytest -p no:cacheprovider
```

Use focused tests while iterating, then run the affected PostgreSQL-backed
suite. Publication changes normally need publication, tablet manifest/API, and
portal tests. API changes should also run `python manage.py spectacular
--validate`.

CI runs three mandatory jobs independently: `static-checks` runs Ruff, mypy,
Bandit, Django system checks and OpenAPI validation; `server-tests` runs the
complete serial PostgreSQL/PostGIS suite, including Linux filesystem/security
tests; `deployment-security` runs production bootstrap, migrations, grants,
privilege-denial/backup verification and deployment helper checks. Migration
drift is checked against that job's initialized database, with connection
warnings treated as errors. Both database jobs provision their own disposable
PostGIS service and verify the required locale. All three jobs must pass.
The independent `dependency-audit` job remains advisory; its successful
completion does not establish that all vulnerabilities are resolved.

## Test selections

Python tests verify server behavior, persisted state, protocols and security.
Do not add assertions for CSS, wording, buttons, navigation, DOM structure,
template fragments or browser appearance. Use response context/form errors for
filtering and validation; keep rendered-output assertions only where they protect
escaping, tenant isolation or secret non-disclosure. Visual and browser interaction
acceptance is manual, outside pytest. Retain functional HTTP/HTMX protocol,
CSRF, privacy/cache-header, CLI diagnostic and redaction coverage.

Use `config.settings.test` and the local `.env` `TEST_POSTGRES_*` values.
The complete PostgreSQL/PostGIS suite is the authoritative regression gate;
no marker filter is configured by default. `tools/` is collected along with
`apps/` and `config/`, including both fake-iPad protocol/CLI test modules.

```powershell
# Focused development checks (choose the modules affected by the change).
python -m pytest --ds=config.settings.test -p no:cacheprovider apps/ingestion/tests/test_services.py apps/publications/tests/test_debounce.py
# Routine: exclude only explicitly marked extended workloads.
python -m pytest --ds=config.settings.test -p no:cacheprovider -m "not slow"
# Complete regression, including extended workloads and tools/.
python -m pytest --ds=config.settings.test -p no:cacheprovider -ra --durations=25
# Extended workloads only.
python -m pytest --ds=config.settings.test -p no:cacheprovider -m slow
```

`slow` currently identifies the 50,000-feature accepted GeoJSON import and
50,000-hydrant summary builder. Rejected-input boundaries remain routine.
Concurrency, authorization, migration, lifecycle and cryptographic contract
checks remain in the complete gate; they are not classified as extended merely
because they require a database. Add `--collect-only -q` to inspect a selection,
`-ra` to report skip reasons, or `--durations=25` to profile a run.

Use rollback-isolated `django_db` for ordinary persisted-state assertions.
Request `transaction=True` explicitly for real commits/callback execution,
separate connections, lock interleavings, flush and migration checks. Do not
replace a commit guarantee with callback capture. Test registry restoration and
immutable audit protections in `config/settings/test.py` must remain intact.
Run pytest serially: root scratch cleanup and PostgreSQL isolation have not
been assessed for parallel workers. Historical migration tests must upgrade
forward in the shared disposable PostGIS-template fixture; do not downgrade
through irreversible migrations. Preserve real commit/visibility guarantees
and negative authorization coverage when consolidating tests.

CI also runs these portable deployment helper checks in its existing Bash step:

```bash
bash deploy/tests/test_application_secret_keyring.sh
bash deploy/tests/test_outbound_mail_proxy_env.sh
bash deploy/tests/test_postgres_utf8.sh
bash deploy/tests/test_release_activation.sh
bash deploy/tests/test_systemd_quiesce.sh
bash deploy/tests/test_verify_django_checks.sh
bash deploy/tests/test_restore_target.sh
```

The signing-key rotation test is a **separate privileged integration check**:
run `bash deploy/tests/test_rotate_publication_signing_key.sh` only in an
isolated, disposable, root-capable Linux environment with the helper's Python
cryptography dependencies available. It is not part of ordinary CI. Exit 77
means the environment is unsupported, not successful verification; report that
check as unverified. Do not elevate ordinary CI to run it.

## Report Transfer development

Read [Report Transfer](report-transfer.md) and the
[tablet contract](tablet-api.md#encrypted-report-transfer). Current focused
coverage lives in `apps/report_transfer/tests/`,
`apps/outbound_mail/tests/test_recipients.py`, `tools/fake_ipad/test_reports.py`
and `tools/test_report_transfer_deployment.py`. Removed PDF-mail admission and
delivery test modules are obsolete. Real tablet/mailbox/browser and native
Debian evidence belongs in [acceptance](acceptance.md#report-transfer-beta-acceptance).
Markdown-only edits normally need link/contract review and `git diff --check`,
not pytest, migrations, Ruff or the full CI command set.

## CI PostgreSQL locale contract

Production and test bootstrap both require exactly `UTF8|C.utf8|C.utf8`;
the verifier rejects other encodings or locale spellings. Existing databases
are never converted or recreated by bootstrap. Owner, runtime, backup and
disposable test roles remain separate.

CI now logs the container's available locales, supplies the missing `C.utf8`
symlink to that existing compiled locale when necessary, and requires both
the exact name and UTF-8 character map before running the unchanged SQL.
The correction affects only the disposable CI container. It neither adopts
the container's `en_US.utf8` default nor loosens production compatibility
checks. Missing locale data or failed verification stops CI. The portable
`test_postgres_utf8.sh` retains the non-mutation checks and explicitly rejects
SQL_ASCII, plain C, en_US.utf8 and the alternative C.UTF-8 database spelling.

Windows cannot execute the container locale probe without a Linux Docker
daemon. A successful candidate Actions run must establish actual libc and
PostgreSQL acceptance, production role/audit denials and backup readability.
Read-only image inspection and Bash/static assertions alone do not establish
that integration result.

## Verification boundaries

The full type gate includes tests and tools. Preserve negative authorization,
real commit/visibility guarantees and scoped malformed-input typing explanations.
Crypto tamper tests must guarantee corruption (for example XOR one bit).
Bandit exceptions must remain narrow and must not suppress credentials or rules
globally. The dated dependency-advisory record and its uncertainty remain in
[security.md](security.md); old local results do not certify the current candidate.
Candidate CI must establish Linux-only behavior and actual PostgreSQL locale,
role-denial and backup integration. Local Windows checks cannot do so.

### Skip accounting

Report these as unsupported or unverified, never as passing integration tests:

| Check | Windows reason | Required environment |
| --- | --- | --- |
| Publication artifact group ownership | Windows cannot verify Unix group ownership | Native Unix with the configured artifact group |
| Five broker path/special-file attacks and directory replacement (six pytest cases) | Linux `dir_fd`, `O_NOFOLLOW`, ownership and FIFO semantics | Native Linux; these must execute in ordinary Linux CI |
| Fake-iPad owner-only state-file mode (one pytest case) | POSIX modes are not Windows ACLs | Native Linux; must execute in Linux CI |
| Release activation atomic-symlink success branch | Git Bash lacks native Unix symlinks | Native Linux portable helper |
| Restore target symlink escape branch | Git Bash lacks native Unix symlinks | Native Linux portable helper |
| Restore positive fixture | Git Bash cannot reproduce Unix `install` modes | Native Linux portable helper |
| State classification current-symlink branch | Git Bash lacks native Unix symlinks | Native Linux portable helper |
| Signing-key rotation helper | Exit 77: isolated root-capable Linux required | Privileged, disposable Linux; excluded from ordinary CI |
| Real systemd units, credentials/write boundaries, sanitizer sandbox, timeouts/cleanup, restore and interrupted deployment | No installed Debian/systemd environment locally; local Docker daemon unavailable | Controlled Debian LXC and the prerequisites in [deployment.md](deployment.md#deployment-hardening-audit-2026-10-19) |

The seven pytest skips are exactly
`apps/reference_data/tests/test_pdf_sanitizer_broker.py::test_native_output_rejects_path_redirection_and_special_files`
with parameters `root-link`, `job-link`, `file-link`, `hard-link`, `fifo`;
`apps/reference_data/tests/test_pdf_sanitizer_broker.py::test_native_directory_replacement_cannot_redirect_finalization`;
and `tools/test_fake_ipad.py::test_secure_write_is_owner_only`.

Portable systemctl fixtures check orchestration control flow only. Neither
passing those helpers nor native broker filesystem tests proves the real
systemd/LXC isolation. These checks do not install a release or exercise host
lifecycle operations.

## Windows pytest scratch space

Pytest is configured to use the fixed repository-local `.test-tmp` scratch
base. The project hook removes only that guarded directory at session start and
finish, so successful runs leave no repository test data and later runs reuse
the same location. Do not supply a feature-named `--basetemp` path (for
example, `.pytest-tmp-foo`): explicit bases are retained by pytest and sandbox
created directories can be inaccessible to a later host process.

On Windows always disable pytest's cache provider:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
pytest -p no:cacheprovider apps/publications/tests/test_artifacts.py
```

If a test run is interrupted and `.test-tmp` cannot be removed by its owner,
leave it in place for the owning environment/administrator rather than
changing ACLs. It is ignored by Git and contains disposable test data only.

## Boundaries to preserve

Keep secrets out of source, fixtures, logs, and test output. Gunicorn must not
load the publication KEK or signing private key. PostgreSQL is the queue; do
not introduce a broker, Redis, or background framework to work around a test.
Read [architecture.md](architecture.md) before changing publication or tablet
state, and [security.md](security.md) before changing credentials or crypto.
