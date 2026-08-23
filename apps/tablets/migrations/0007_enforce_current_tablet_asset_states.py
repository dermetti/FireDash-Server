"""Narrow physical Tablet asset states after legacy rows have been removed."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tablets", "0006_remove_removed_tablet_data")]

    operations = [
        migrations.AlterField(
            model_name="tablet",
            name="status",
            field=models.CharField(
                choices=[
                    ("INACTIVE", "Inactive"),
                    ("ACTIVE", "Active"),
                    ("LOST", "Lost"),
                    ("RETIRED", "Retired"),
                ],
                default="INACTIVE",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="tablet",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ("INACTIVE", "ACTIVE", "LOST", "RETIRED"))
                ),
                name="tablet_status_is_current_asset_state",
            ),
        ),
        migrations.RemoveField(model_name="tablet", name="removed_at"),
        migrations.RemoveField(model_name="tablet", name="removed_by"),
    ]
