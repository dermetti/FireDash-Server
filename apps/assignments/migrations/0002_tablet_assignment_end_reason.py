from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assignments", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="tabletvehicleassignment",
            name="end_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REASSIGNED", "Reassigned"),
                    ("VEHICLE_RETIRED", "Vehicle retired"),
                ],
                max_length=32,
            ),
        )
    ]
