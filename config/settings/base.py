from pathlib import Path

from config.settings.env import get_env, get_list

BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = get_env("DJANGO_SECRET_KEY", default="")
DEBUG = False
ALLOWED_HOSTS = get_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "apps.accounts",
    "apps.organizations",
    "apps.personnel",
    "apps.tablets",
    "apps.assignments",
    "apps.authorization",
    "apps.audit",
    "apps.portal",
    "apps.health",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.audit.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = get_env("DJANGO_STATIC_ROOT", default="/var/lib/fire-backend/static")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_AGE = int(get_env("ADMIN_SESSION_MAX_AGE_SECONDS", default="28800"))
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = get_list("DJANGO_CSRF_TRUSTED_ORIGINS")

PRE_MFA_SESSION_MAX_AGE_SECONDS = int(get_env("PRE_MFA_SESSION_MAX_AGE_SECONDS", default="600"))
AUTH_THROTTLE_MAX_FAILURES = int(get_env("AUTH_THROTTLE_MAX_FAILURES", default="5"))
AUTH_THROTTLE_WINDOW_SECONDS = int(get_env("AUTH_THROTTLE_WINDOW_SECONDS", default="900"))
AUTH_THROTTLE_LOCKOUT_SECONDS = int(get_env("AUTH_THROTTLE_LOCKOUT_SECONDS", default="900"))
RECENT_REAUTH_MAX_AGE_SECONDS = int(get_env("RECENT_REAUTH_MAX_AGE_SECONDS", default="900"))

# Gunicorn is reachable only through the local Nginx Unix socket.
TRUSTED_PROXY_IPS = frozenset(get_list("TRUSTED_PROXY_IPS", default=("127.0.0.1", "::1")))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
