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

Local development uses `config.settings.development`. Tests use `config.settings.test` and require a disposable PostgreSQL/PostGIS database. Production uses `config.settings.production` and must receive all required values through `/etc/fire-backend/fire-backend.env`.
