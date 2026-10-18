# Development

## Local environment

Use Python 3.13 or the repository-supported interpreter, PostgreSQL with
PostGIS, and a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements\development.txt
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
mypy apps/publications apps/tablets apps/portal
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
python -m pytest --ds=config.settings.test -p no:cacheprovider
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
```

The signing-key rotation test is a **separate privileged integration check**:
run `bash deploy/tests/test_rotate_publication_signing_key.sh` only in an
isolated, disposable, root-capable Linux environment with the helper's Python
cryptography dependencies available. It is not part of ordinary CI. Exit 77
means the environment is unsupported, not successful verification; report that
check as unverified. Do not elevate ordinary CI to run it.

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
