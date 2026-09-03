"""One-off clean PostgreSQL settings for dangerous-goods integration tests."""

from config.settings.test import *  # noqa: F403

# A disposable PostGIS-only clone: it contains the extension but no FireDash
# application tables, unlike the shared application-table template.
DATABASES["default"]["TEST"] = {"TEMPLATE": "dangerous_goods_postgis_clean"}  # noqa: F405
