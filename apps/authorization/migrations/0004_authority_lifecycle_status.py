import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def migrate_legacy_authority_state(apps, schema_editor):
    DepartmentMembership = apps.get_model("authorization", "DepartmentMembership")
    StationAdminAssignment = apps.get_model("authorization", "StationAdminAssignment")
    DepartmentMembership.objects.filter(active=True).update(status="ACTIVE")
    DepartmentMembership.objects.filter(active=False).update(status="REVOKED")
    StationAdminAssignment.objects.filter(active=True).update(status="ACTIVE")
    StationAdminAssignment.objects.filter(active=False).update(status="REVOKED")


class Migration(migrations.Migration):
    dependencies = [
        ("authorization", "0003_api_version_compatibility_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="departmentmembership",
            name="status",
            field=models.CharField(
                choices=[("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("REVOKED", "Revoked")],
                default="ACTIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="stationadminassignment",
            name="status",
            field=models.CharField(
                choices=[("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("REVOKED", "Revoked")],
                default="ACTIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="departmentmembership",
            name="suspended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="departmentmembership",
            name="suspended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="suspended_department_memberships",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="stationadminassignment",
            name="suspended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="stationadminassignment",
            name="suspended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="suspended_station_admin_assignments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(migrate_legacy_authority_state, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="departmentmembership", name="one_active_department_role"
        ),
        migrations.RemoveConstraint(
            model_name="departmentmembership", name="one_active_department_admin"
        ),
        migrations.RemoveConstraint(
            model_name="departmentmembership", name="department_membership_revocation_state"
        ),
        migrations.RemoveConstraint(
            model_name="stationadminassignment", name="one_active_station_admin_assignment"
        ),
        migrations.RemoveConstraint(
            model_name="stationadminassignment", name="station_assignment_revocation_state"
        ),
        migrations.RemoveField(model_name="departmentmembership", name="active"),
        migrations.RemoveField(model_name="stationadminassignment", name="active"),
        migrations.AddConstraint(
            model_name="departmentmembership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "ACTIVE")),
                fields=("user", "department", "role"),
                name="one_active_department_role",
            ),
        ),
        migrations.AddConstraint(
            model_name="departmentmembership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "ACTIVE")),
                fields=("user", "role"),
                name="one_active_department_admin",
            ),
        ),
        migrations.AddConstraint(
            model_name="departmentmembership",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status__in", ("ACTIVE", "SUSPENDED")),
                        ("revoked_at__isnull", True),
                        ("revoked_by__isnull", True),
                    ),
                    models.Q(
                        ("status", "REVOKED"),
                        ("revoked_at__isnull", False),
                        ("revoked_by__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="department_membership_lifecycle_provenance",
            ),
        ),
        migrations.AddConstraint(
            model_name="stationadminassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "ACTIVE")),
                fields=("user", "station"),
                name="one_active_station_admin_assignment",
            ),
        ),
        migrations.AddConstraint(
            model_name="stationadminassignment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status__in", ("ACTIVE", "SUSPENDED")),
                        ("revoked_at__isnull", True),
                        ("revoked_by__isnull", True),
                    ),
                    models.Q(
                        ("status", "REVOKED"),
                        ("revoked_at__isnull", False),
                        ("revoked_by__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="station_assignment_lifecycle_provenance",
            ),
        ),
    ]
