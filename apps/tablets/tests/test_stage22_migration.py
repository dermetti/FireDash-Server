"""Forward-data migration coverage for removing legacy REMOVED tablet rows."""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import AdoptionInvitation, Tablet


@pytest.mark.django_db(transaction=True)
def test_stage22_migration_deletes_only_removed_tablet_dependents():
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    before_targets = [
        ("tablets", "0005_alter_tablet_status") if app == "tablets" else (app, migration)
        for app, migration in latest_targets
    ]
    executor.migrate(before_targets)
    try:
        old_apps = executor.loader.project_state(before_targets).apps
        OldTablet = old_apps.get_model("tablets", "Tablet")
        OldAssignment = old_apps.get_model("assignments", "TabletVehicleAssignment")
        OldInvitation = old_apps.get_model("tablets", "AdoptionInvitation")

        user = User.objects.create_user("migration@example.test", "Migration", "safe-password")
        department = Department.objects.create(name="Migration", short_code="MIG", created_by=user)
        station = Station.objects.create(department=department, name="Station", short_code="MIG")
        vehicle = Vehicle.objects.create(
            department=department, station=station, display_name="Engine"
        )
        removed = OldTablet.objects.create(
            department_id=department.id,
            display_name="Removed",
            asset_number="",
            status="REMOVED",
            active=False,
        )
        retained = OldTablet.objects.create(
            department_id=department.id,
            display_name="Retained",
            asset_number="",
            status="INACTIVE",
            active=True,
        )
        OldAssignment.objects.create(
            tablet_id=removed.id,
            vehicle_id=vehicle.id,
            valid_from=timezone.now(),
            created_by_id=user.id,
        )
        invitation = OldInvitation.objects.create(
            tablet_id=removed.id,
            token_hash="a" * 64,
            expires_at=timezone.now() + timedelta(minutes=15),
            created_by_id=user.id,
        )

        connection.commit()
        executor = MigrationExecutor(connection)
        executor.migrate(latest_targets)

        assert not Tablet.objects.filter(pk=removed.id).exists()
        assert Tablet.objects.filter(pk=retained.id, status=Tablet.Status.INACTIVE).exists()
        assert not AdoptionInvitation.objects.filter(pk=invitation.id).exists()
    finally:
        connection.commit()
        executor = MigrationExecutor(connection)
        executor.migrate(latest_targets)
