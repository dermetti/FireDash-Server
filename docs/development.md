# Local Development

Create a Python virtual environment, install `requirements/dev.txt`, and run PostgreSQL with PostGIS locally. Copy `.env.example` to `.env` for your shell environment; do not commit it.

```text
python manage.py migrate
python manage.py runserver
pytest
ruff check .
mypy apps config manage.py
bandit -r apps config manage.py --exclude "*/tests/*,*/migrations/*"
pip-audit -r requirements/base.txt
```

Local development uses `config.settings.development`. Tests use `config.settings.test` and require
the dedicated `firedash_test` role through `TEST_POSTGRES_USER`, `TEST_POSTGRES_PASSWORD`,
`TEST_POSTGRES_HOST`, `TEST_POSTGRES_PORT`, and optional `TEST_POSTGRES_DB`. The role has
`CREATEDB` solely to create disposable databases from the non-connectable
`firedash_test_template` PostGIS template. Create both with
`deploy/postgresql/bootstrap-test.sql`; never grant `CREATEDB` to an application runtime or production
role. Production uses `config.settings.production` and must receive all required values through
`/etc/fire-backend/fire-backend.env`.
