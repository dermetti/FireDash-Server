from config.settings.base import *  # noqa: F403
from config.settings.env import get_typed_env

DEBUG = False

if not SECRET_KEY:  # noqa: F405
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production.")

if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production.")

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": get_env("POSTGRES_DB", required=True),  # noqa: F405
        "USER": get_env("POSTGRES_USER", required=True),  # noqa: F405
        "PASSWORD": get_env("POSTGRES_PASSWORD", required=True),  # noqa: F405
        "HOST": get_env("POSTGRES_HOST", required=True),  # noqa: F405
        "PORT": get_typed_env("POSTGRES_PORT", int, default=5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
