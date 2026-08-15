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
pytest
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
pytest
```

Use focused tests while iterating, then run the affected PostgreSQL-backed
suite. Publication changes normally need publication, tablet manifest/API, and
portal tests. API changes should also run `python manage.py spectacular
--validate`.

## Boundaries to preserve

Keep secrets out of source, fixtures, logs, and test output. Gunicorn must not
load the publication KEK or signing private key. PostgreSQL is the queue; do
not introduce a broker, Redis, or background framework to work around a test.
Read [architecture.md](architecture.md) before changing publication or tablet
state, and [security.md](security.md) before changing credentials or crypto.
