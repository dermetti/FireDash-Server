from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tablets", "0008_remove_obsolete_reactivation_flow")]

    operations = [
        migrations.AddField(
            model_name="appinstallation",
            name="free_storage_bytes",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appinstallation",
            name="storage_observed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appinstallation",
            name="total_storage_bytes",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="appinstallation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("free_storage_bytes__isnull", True),
                        ("storage_observed_at__isnull", True),
                        ("total_storage_bytes__isnull", True),
                    ),
                    models.Q(
                        ("free_storage_bytes__isnull", False),
                        ("storage_observed_at__isnull", False),
                        ("total_storage_bytes__gt", 0),
                        ("total_storage_bytes__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="installation_storage_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="appinstallation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("free_storage_bytes__isnull", True),
                    ("free_storage_bytes__lte", models.F("total_storage_bytes")),
                    _connector="OR",
                ),
                name="installation_storage_free_lte_total",
            ),
        ),
    ]
