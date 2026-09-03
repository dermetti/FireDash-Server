import os
from pathlib import Path
from urllib.parse import urlsplit

from config.settings.env import EnvironmentConfigurationError, get_env, get_list, get_typed_env

BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = get_env("DJANGO_SECRET_KEY", default="")
DEBUG = False
ALLOWED_HOSTS = get_list("DJANGO_ALLOWED_HOSTS")
FIREDASH_PUBLIC_ORIGIN = get_env("FIREDASH_PUBLIC_ORIGIN", default="")


def validate_public_origin(origin: str) -> None:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("FIREDASH_PUBLIC_ORIGIN must be an HTTPS origin without a path.")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "apps.accounts",
    "apps.organizations",
    "apps.personnel",
    "apps.publications",
    "apps.reference_data",
    "apps.tablets",
    "apps.assignments",
    "apps.authorization",
    "apps.audit",
    "apps.ingestion",
    "apps.portal",
    "apps.health",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.audit.middleware.RequestContextMiddleware",
    "apps.accounts.middleware.ReauthRedirectMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.cache.AuthenticatedNoStoreMiddleware",
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
                "django.template.context_processors.csrf",
                "apps.portal.context_processors.navigation",
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
STATICFILES_DIRS = [BASE_DIR / "static"]
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

REFERENCE_DATA_QUARANTINE_ROOT = Path(
    get_env("REFERENCE_DATA_QUARANTINE_ROOT", default="/var/lib/fire-backend/quarantine")
)
REFERENCE_DATA_SANITIZER_OUTPUT_ROOT = Path(
    get_env(
        "REFERENCE_DATA_SANITIZER_OUTPUT_ROOT", default="/var/lib/fire-backend/sanitizer-output"
    )
)
REFERENCE_DATA_ACCEPTED_ROOT = Path(
    get_env("REFERENCE_DATA_ACCEPTED_ROOT", default="/var/lib/fire-backend/fire-plans")
)
# Production config sets this to the narrow publication-source reader group.
# Empty keeps local development and isolated tests free of host account setup.
REFERENCE_DATA_ACCEPTED_GROUP = get_env("REFERENCE_DATA_ACCEPTED_GROUP", default="")
INGESTION_STAGING_ROOT = Path(
    get_env("INGESTION_STAGING_ROOT", default="/var/lib/fire-backend/import-staging")
)
MAX_STRUCTURED_IMPORT_BYTES = get_typed_env(
    "MAX_STRUCTURED_IMPORT_BYTES", int, default=20 * 1024 * 1024
)
MAX_STRUCTURED_IMPORT_ROWS = get_typed_env("MAX_STRUCTURED_IMPORT_ROWS", int, default=20_000)
# Hydrant GeoJSON features are a separate, higher hard limit than CSV/JSON rows;
# one authoritative snapshot may contain tens of thousands of hydrants.
MAX_HYDRANT_GEOJSON_FEATURES = get_typed_env("MAX_HYDRANT_GEOJSON_FEATURES", int, default=50_000)
MAX_IMPORT_VALIDATION_ERRORS = get_typed_env("MAX_IMPORT_VALIDATION_ERRORS", int, default=200)
# Aggregate upload/request ceiling for a Fire Plan/KLGV ZIP package. Individual
# PDFs remain bounded separately by MAX_PDF_INPUT_BYTES.
MAX_INGEST_UPLOAD_BYTES = get_typed_env("MAX_INGEST_UPLOAD_BYTES", int, default=256 * 1024 * 1024)
MAX_PDF_PACKAGE_EXPANDED_BYTES = get_typed_env(
    "MAX_PDF_PACKAGE_EXPANDED_BYTES", int, default=512 * 1024 * 1024
)
MAX_PDF_PACKAGE_DOCUMENTS = get_typed_env("MAX_PDF_PACKAGE_DOCUMENTS", int, default=250)
IMPORT_PREVIEW_RETENTION_DAYS = get_typed_env("IMPORT_PREVIEW_RETENTION_DAYS", int, default=7)
IMPORT_APPLIED_SOURCE_RETENTION_DAYS = get_typed_env(
    "IMPORT_APPLIED_SOURCE_RETENTION_DAYS", int, default=30
)
MAX_PDF_INPUT_BYTES = get_typed_env("MAX_PDF_INPUT_BYTES", int, default=100 * 1024 * 1024)
MAX_PDF_OUTPUT_BYTES = get_typed_env("MAX_PDF_OUTPUT_BYTES", int, default=150 * 1024 * 1024)
MAX_PDF_PAGES = get_typed_env("MAX_PDF_PAGES", int, default=500)
MAX_HYDRANT_IMPORT_FEATURES = get_typed_env("MAX_HYDRANT_IMPORT_FEATURES", int, default=20_000)
PDF_SANITIZER_TIMEOUT_SECONDS = get_typed_env("PDF_SANITIZER_TIMEOUT_SECONDS", int, default=60)
PDF_SANITIZER_MEMORY_MAX_BYTES = get_typed_env(
    "PDF_SANITIZER_MEMORY_MAX_BYTES", int, default=512 * 1024 * 1024
)
PDF_SANITIZER_BROKER_SOCKET = get_env(
    "PDF_SANITIZER_BROKER_SOCKET", default="/run/fire-pdf-sanitizer-broker/broker.sock"
)
PUBLICATION_WORKER_BATCH_SIZE = get_typed_env("PUBLICATION_WORKER_BATCH_SIZE", int, default=10)
PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS = get_typed_env(
    "PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS", int, default=300
)
PUBLICATION_JOB_MAX_ATTEMPTS = get_typed_env("PUBLICATION_JOB_MAX_ATTEMPTS", int, default=3)
PUBLICATION_DATA_CHANGE_DEBOUNCE_SECONDS = get_typed_env(
    "PUBLICATION_DATA_CHANGE_DEBOUNCE_SECONDS", int, default=120
)
PUBLICATION_DATA_CHANGE_MAX_DEFERRAL_SECONDS = get_typed_env(
    "PUBLICATION_DATA_CHANGE_MAX_DEFERRAL_SECONDS", int, default=600
)
PUBLICATION_BUILD_WAKE_SOCKET_PATH = get_typed_env(
    "PUBLICATION_BUILD_WAKE_SOCKET_PATH", str, default="/run/fire-backend/publication-build.sock"
)
PUBLICATION_BUILD_WAKE_TIMEOUT_SECONDS = get_typed_env(
    "PUBLICATION_BUILD_WAKE_TIMEOUT_SECONDS", float, default=1.0
)
TEMPORARY_ASSIGNMENT_EXPIRY_BATCH_SIZE = get_typed_env(
    "TEMPORARY_ASSIGNMENT_EXPIRY_BATCH_SIZE", int, default=100
)
PUBLICATION_BUILD_SUMMARY_MAX_ITEMS = get_typed_env(
    "PUBLICATION_BUILD_SUMMARY_MAX_ITEMS", int, default=50_000
)
SIGNED_MANIFEST_RETENTION_DAYS = get_typed_env("SIGNED_MANIFEST_RETENTION_DAYS", int, default=30)
PUBLICATION_ARTIFACT_ROOT = Path(
    get_env("PUBLICATION_ARTIFACT_ROOT", default="/var/lib/fire-backend/publications")
)
PUBLICATION_ARTIFACT_TEMP_ROOT = Path(
    get_env("PUBLICATION_ARTIFACT_TEMP_ROOT", default="/var/lib/fire-backend/publications/.tmp")
)


def validate_publication_artifact_layout(*, root: Path, temp_root: Path) -> None:
    """Fail fast if temp artifacts would live outside the atomic-promotion root.

    Atomic `os.replace` promotion requires temp and final artifacts to share one
    writable filesystem tree, so the temp root must be a strict descendant of the
    artifact root. Normalization is lexical (no filesystem resolution) to keep the
    check side-effect free during settings load.
    """
    normalized_root = Path(os.path.normpath(str(root)))
    normalized_temp = Path(os.path.normpath(str(temp_root)))
    if normalized_temp == normalized_root or not normalized_temp.is_relative_to(normalized_root):
        raise EnvironmentConfigurationError(
            "PUBLICATION_ARTIFACT_TEMP_ROOT must be a strict descendant of "
            "PUBLICATION_ARTIFACT_ROOT so artifact promotion remains an atomic "
            "same-mount rename."
        )


validate_publication_artifact_layout(
    root=PUBLICATION_ARTIFACT_ROOT, temp_root=PUBLICATION_ARTIFACT_TEMP_ROOT
)
# The plaintext publication bundle ceiling. TEMPORARY compatibility value only:
# department Fire Plans are still built as one monolithic ZIP of every active
# plan (``_artifact_fire_plans``), so the ceiling is sized to the single-package
# ingestion envelope (MAX_PDF_PACKAGE_EXPANDED_BYTES = 512 MiB + framing/AEAD
# overhead), NOT to the ~2,800-plan production target. The scale solution is the
# per-document architecture (signed generation manifest + immutable individual
# encrypted PDF artifacts); do not treat a larger monolithic ceiling as scalable.
PUBLICATION_ARTIFACT_MAX_BYTES = get_typed_env(
    "PUBLICATION_ARTIFACT_MAX_BYTES", int, default=600 * 1024 * 1024
)
PUBLICATION_ARTIFACT_STALE_SECONDS = get_typed_env(
    "PUBLICATION_ARTIFACT_STALE_SECONDS", int, default=3600
)
PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS = get_typed_env(
    "PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS", int, default=2
)
PUBLICATION_TERMINAL_SNAPSHOT_RETENTION_DAYS = get_typed_env(
    "PUBLICATION_TERMINAL_SNAPSHOT_RETENTION_DAYS", int, default=30
)
PUBLICATION_RETENTION_BATCH_SIZE = get_typed_env(
    "PUBLICATION_RETENTION_BATCH_SIZE", int, default=100
)


def publication_credential_path(*, override_name: str, credential_name: str) -> Path:
    """Return an explicit override or this unit's systemd credential path.

    ``LoadCredential=`` gives each service invocation its own directory.  The
    directory name is intentionally not stable, so workers must use systemd's
    ``CREDENTIALS_DIRECTORY`` rather than a particular unit name.  Explicit
    paths remain available for development and focused tests.
    """
    override = os.environ.get(override_name)
    if override:
        return Path(override)
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials_directory:
        return Path(credentials_directory) / credential_name
    # Outside a systemd credential context the credential is deliberately
    # unavailable unless the caller supplies an explicit override.
    return Path("/run/credentials") / credential_name


PUBLICATION_KEK_CREDENTIAL_PATH = publication_credential_path(
    override_name="PUBLICATION_KEK_CREDENTIAL_PATH", credential_name="publication-kek"
)
PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH = publication_credential_path(
    override_name="PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH",
    credential_name="publication-signing-key",
)
PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH = publication_credential_path(
    override_name="PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH",
    credential_name="publication-signing-public-key-ring",
)
PUBLICATION_KEK_VERSION = get_env("PUBLICATION_KEK_VERSION", default="1")
PUBLICATION_SIGNING_KEY_VERSION = get_env("PUBLICATION_SIGNING_KEY_VERSION", default="1")

# Gunicorn is reachable only through the local Nginx Unix socket.
TRUSTED_PROXY_IPS = frozenset(get_list("TRUSTED_PROXY_IPS", default=("127.0.0.1", "::1")))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "config.api.problem_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "FireDash Provisioning API",
    "VERSION": "1.0.0",
    "OAS_VERSION": "3.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
