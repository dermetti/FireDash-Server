from config.settings.base import *  # noqa: F403
from config.settings.base import BASE_DIR
from config.settings.env import get_bool, get_env, get_typed_env

DEBUG = get_bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
SECRET_KEY = get_env("DJANGO_SECRET_KEY", default="development-only-not-for-production")
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
STATIC_ROOT = str(BASE_DIR / "staticfiles")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": get_env("POSTGRES_DB", default="fire_backend"),
        "USER": get_env("POSTGRES_USER", default="application_runtime"),
        "PASSWORD": get_env("POSTGRES_PASSWORD", default="fire_backend_dev"),
        "HOST": get_env("POSTGRES_HOST", default="127.0.0.1"),
        "PORT": get_typed_env("POSTGRES_PORT", int, default=5432),
    }
}
