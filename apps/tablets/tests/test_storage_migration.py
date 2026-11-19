"""Permanent tablet telemetry constraints and lifecycle schema."""

import pytest
from django.db import connection

from apps.tablets.models import Tablet


@pytest.mark.django_db
def test_storage_baseline_has_complete_telemetry_and_current_lifecycle():
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, "tablets_appinstallation")
        constraints = connection.introspection.get_constraints(cursor, "tablets_appinstallation")
    assert {"free_storage_bytes", "total_storage_bytes", "storage_observed_at"} <= {
        column.name for column in columns
    }
    assert constraints["installation_storage_complete"]["check"]
    assert constraints["installation_storage_free_lte_total"]["check"]
    assert set(Tablet.Status.values) == {"ACTIVE", "INACTIVE", "LOST", "RETIRED"}
    assert not any("reactivation" in table for table in connection.introspection.table_names())
