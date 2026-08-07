from config.settings.development import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key-not-for-production"  # nosec B105
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
