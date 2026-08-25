from django.db import migrations, models

DEPARTMENT_MEMBERSHIP_LIFECYCLE_PROVENANCE = models.CheckConstraint(
    condition=(
        models.Q(
            status__in=("ACTIVE", "SUSPENDED"),
            revoked_at__isnull=True,
            revoked_by__isnull=True,
        )
        | models.Q(
            status="REVOKED",
            revoked_at__isnull=False,
            revoked_by__isnull=False,
        )
    ),
    name="department_membership_lifecycle_provenance",
)

STATION_ASSIGNMENT_LIFECYCLE_PROVENANCE = models.CheckConstraint(
    condition=(
        models.Q(
            status__in=("ACTIVE", "SUSPENDED"),
            revoked_at__isnull=True,
            revoked_by__isnull=True,
        )
        | models.Q(
            status="REVOKED",
            revoked_at__isnull=False,
            revoked_by__isnull=False,
        )
    ),
    name="station_assignment_lifecycle_provenance",
)


class Migration(migrations.Migration):
    """Align migration state with an already-equivalent live check constraint."""

    dependencies = [("authorization", "0004_authority_lifecycle_status")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="departmentmembership",
                    name="department_membership_lifecycle_provenance",
                ),
                migrations.RemoveConstraint(
                    model_name="stationadminassignment",
                    name="station_assignment_lifecycle_provenance",
                ),
                migrations.AddConstraint(
                    model_name="departmentmembership",
                    constraint=DEPARTMENT_MEMBERSHIP_LIFECYCLE_PROVENANCE,
                ),
                migrations.AddConstraint(
                    model_name="stationadminassignment",
                    constraint=STATION_ASSIGNMENT_LIFECYCLE_PROVENANCE,
                ),
            ],
        )
    ]
