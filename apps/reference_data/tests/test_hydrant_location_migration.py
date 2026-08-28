"""Migration coverage for Hydrant's geometry/descriptive-location split."""

import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import Hydrant


@pytest.mark.django_db(transaction=True)
def test_hydrant_location_migration_preserves_point_and_leaves_text_location_null():
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    before_targets = [
        ("reference_data", "0012_finalize_klgv_required_metadata")
        if app == "reference_data"
        else (app, migration)
        for app, migration in latest_targets
    ]
    executor.migrate(before_targets)
    try:
        old_apps = executor.loader.project_state(before_targets).apps
        OldHydrant = old_apps.get_model("reference_data", "Hydrant")
        user = User.objects.create_user("migration@example.test", "Migration", "safe-password")
        department = Department.objects.create(name="Migration", short_code="MIG", created_by=user)
        original_point = Point(8.682127, 50.110924, srid=4326)
        old_hydrant = OldHydrant.objects.create(
            department_id=department.id,
            external_identifier="H-MIG-1",
            location=original_point,
            status="ACTIVE",
            source_metadata={},
        )
        original_ewkb = old_hydrant.location.ewkb

        connection.commit()
        executor = MigrationExecutor(connection)
        executor.migrate(latest_targets)

        hydrant = Hydrant.objects.get(pk=old_hydrant.pk)
        assert hydrant.location is None
        assert hydrant.geometry.ewkb == original_ewkb
        payload = build_source_payload(
            definition=get_dataset_definition("department_hydrants"),
            department=department,
            station=None,
        )
        assert payload["features"][0]["geometry"] == {
            "type": "Point",
            "coordinates": [8.682127, 50.110924],
        }
    finally:
        connection.commit()
        executor = MigrationExecutor(connection)
        executor.migrate(latest_targets)
