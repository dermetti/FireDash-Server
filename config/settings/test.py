from django.db import connections
from django.db.models.signals import post_migrate

from config.settings.development import *  # noqa: F403
from config.settings.env import get_env, get_typed_env

DEBUG = False
SECRET_KEY = "test-secret-key-not-for-production"  # nosec B105
FIREDASH_PUBLIC_ORIGIN = "https://firedash.test"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# pytest-django must never inherit the development/runtime database role.
DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": get_env("TEST_POSTGRES_DB", default="test_firedash"),
        "USER": get_env("TEST_POSTGRES_USER", required=True),
        "PASSWORD": get_env("TEST_POSTGRES_PASSWORD", required=True),
        "HOST": get_env("TEST_POSTGRES_HOST", default="127.0.0.1"),
        "PORT": get_typed_env("TEST_POSTGRES_PORT", int, default=5432),
        "TEST": {"TEMPLATE": "firedash_test_template"},
    }
}


def _restore_registry_projection_for_test_flush(**kwargs) -> None:
    """Django flush is test-only; production projection changes require migrations."""
    app_config = kwargs["app_config"]
    if app_config.label != "publications":
        return
    from apps.publications.models import DatasetTypeRegistry
    from apps.publications.registry import DATASET_REGISTRY

    for definition in DATASET_REGISTRY.values():
        DatasetTypeRegistry.objects.using(kwargs["using"]).update_or_create(
            code=definition.code,
            defaults={
                "scope": definition.scope,
                "current_schema_version": definition.current_schema_version,
                "supported_schema_versions": list(definition.supported_schema_versions),
                "required": definition.required,
                "feature_code": definition.feature_code,
            },
        )


def _allow_audit_flush_for_tests(**kwargs) -> None:
    """Keep append-only audit protection while allowing Django test cleanup."""
    if kwargs["app_config"].label != "audit":
        return
    connection = connections[kwargs["using"]]
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event;
            CREATE TRIGGER audit_event_immutable
            BEFORE UPDATE OR DELETE ON audit_event
            FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation();
            """
        )


post_migrate.connect(
    _restore_registry_projection_for_test_flush,
    dispatch_uid="test.restore_publications_registry_projection",
)
post_migrate.connect(
    _allow_audit_flush_for_tests,
    dispatch_uid="test.allow_audit_flush",
)
