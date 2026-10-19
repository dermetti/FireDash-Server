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

## Test selections

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
been assessed for parallel workers.

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

## CI PostgreSQL locale contract

Production and test bootstrap both require exactly `UTF8|C.utf8|C.utf8`;
the verifier rejects other encodings or locale spellings. Existing databases
are never converted or recreated by bootstrap. Owner, runtime, backup and
disposable test roles remain separate.

[Actions run 39220619434](https://github.com/dermetti/FireDash-Server/actions/runs/39220619434)
failed before migrations and all subsequent gates at `CREATE DATABASE` with
`invalid LC_COLLATE locale name: "C.utf8"`. Its `postgis/postgis:17-3.5`
image digest was
`sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6`.
The image history identifies Debian Bullseye and generation of `en_US.utf8`;
inspection of its exact Debian base layer found compiled locale files under
`/usr/lib/locale/C.UTF-8`, without `/usr/lib/locale/C.utf8`.

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

## Regression stabilization notes

The full CI type gate includes tests and tools. Dynamic JSON/template context
annotations must describe their heterogeneous contents; typed model IDs or
validated state may be narrowed at their existing invariant boundaries.
Keep explicit `None` assertions for idle workers and explicit required-state
assertions for successful worker jobs. Negative and cross-tenant assertions
must remain intact. Deliberately malformed test inputs and partial test
doubles need narrowly scoped typing explanations rather than gate exclusions.

Crypto tamper tests must guarantee a byte changes: XOR one bit instead of
replacing a random ciphertext byte with a constant. The old suffix `b"x"`
could equal the original byte and cause an intermittent `InvalidTag` assertion
failure. Both Fire Plan and KLGV delivery tests now retain that assertion with
guaranteed corruption.

The 2026-10-19 dependency audit of both requirements files reported nine
advisories. Only the three affected runtime pins were raised to reported fixed
versions; no advisory was ignored:

| Dependency | Previous pin | Corrected pin | Reported advisories |
| --- | --- | --- | --- |
| Django | 5.2.16 | [5.2.17](https://pypi.org/project/Django/5.2.17/) | PYSEC-2026-3717, PYSEC-2026-4035 |
| pypdf | 6.18.0 | [6.19.0](https://pypi.org/project/pypdf/6.19.0/) | PYSEC-2026-4154, PYSEC-2026-4160, PYSEC-2026-4159, PYSEC-2026-4156, PYSEC-2026-4155, PYSEC-2026-4157 |
| requests | 2.32.5 | [2.33.0](https://pypi.org/project/requests/2.33.0/) | PYSEC-2026-2275 |

Bandit annotations are restricted to reviewed false positives: empty/cleared
credentials and empty-value database constraints, an encryption-context label,
internal query/form assertions, and best-effort connection close after a failed
SMTP probe. They do not disable scan rules globally or suppress real credentials.
Formatting includes existing migration files; their parsed Python ASTs were
verified unchanged, and migration drift is checked separately.

Local validation used Windows Python 3.13.5 and the configured disposable test
database on PostgreSQL 16.14 / PostGIS 3.4. Its encoding/locale is exactly
`UTF8|C.utf8|C.utf8`; the test role has CREATEDB and no superuser, CREATEROLE,
replication or BYPASSRLS privileges. CI's PostgreSQL 17 / PostGIS 3.5 production
bootstrap/grants/audit-denial/backup checks remain a separate required result.
No role, encoding, existing database or host configuration was changed locally.

Both final requirements files were audited with full dependency resolution and
reported no known vulnerabilities. Windows auditing used an isolated temporary
pip/audit cache and explicit `https://pypi.org/simple` access after the default
host cache was inaccessible; no ACL or TLS verification was relaxed. Ordinary
CI retains its existing audit commands. Ruff formatting/lint, `mypy .` (native
Windows and Linux platform selection), Bandit, Django system/migration checks,
OpenAPI validation, `pip check`, 39 Bash syntax checks, broker compilation and
all 17 CI Bash run-block syntax checks passed locally. The 19 portable helper
scripts passed; their individual unsupported branches are listed below.

The final complete serial command was
`python -m pytest --ds=config.settings.test -p no:cacheprovider -ra --durations=25`:
**1,262 passed, seven skipped, 611.84 seconds (10:11)**, 1,269 collected,
with no marker/path exclusion. This includes both `slow` cases (50,000-feature
GeoJSON acceptance and the 50,000-hydrant summary) and both fake-iPad modules
under `tools/`. The summary's call took 11.66 seconds. The focused encryption
admission contract also passed all six tests after updating its exact-version
assertion to pypdf 6.19.0; spoofed/incomplete encryption rejection remains tested.
The idle-worker `None` assertion remains unchanged. Earlier failed/intermediate
runs do not count as the final baseline.

These are local working-tree results. GitHub Actions must still pass against the
published candidate SHA, including the real container locale probe and Linux-only
branches. A result for an older SHA cannot certify these changes. Repository Git
instructions require explicit authorization before committing/pushing; no commit
or clean-working-tree claim is implied by local test success.

### Skip accounting

Report these as unsupported or unverified, never as passing integration tests:

| Check | Windows reason | Required environment |
| --- | --- | --- |
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
systemd/LXC isolation. Package 4 does not install a release or run those host
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
