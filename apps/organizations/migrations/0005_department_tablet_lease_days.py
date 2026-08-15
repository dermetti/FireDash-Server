from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0004_remove_station_address")]

    operations = [
        migrations.AddField(
            model_name="department",
            name="tablet_lease_days",
            field=models.PositiveSmallIntegerField(default=7, validators=[MinValueValidator(3)]),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.CheckConstraint(
                condition=models.Q(tablet_lease_days__gte=3),
                name="department_tablet_lease_days_min",
            ),
        ),
    ]
