from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0005_department_tablet_lease_days")]

    operations = [
        migrations.AddField(
            model_name="department",
            name="tablet_asset_number_auto_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="department",
            name="tablet_asset_number_prefix",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="department",
            name="tablet_asset_number_sequence",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="department",
            name="tablet_asset_number_width",
            field=models.PositiveSmallIntegerField(
                default=1, validators=[MinValueValidator(1), MaxValueValidator(20)]
            ),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    tablet_asset_number_width__gte=1,
                    tablet_asset_number_width__lte=20,
                ),
                name="department_tablet_asset_number_width_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.CheckConstraint(
                condition=models.Q(tablet_asset_number_sequence__gte=0),
                name="department_tablet_asset_number_sequence_nonnegative",
            ),
        ),
    ]
