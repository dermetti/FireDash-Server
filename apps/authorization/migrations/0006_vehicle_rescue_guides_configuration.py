import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.authorization.models

DEFAULT_WEB_URL = "https://rescue.euroncap.com/"


def create_default_configuration(apps, schema_editor):
    configuration = apps.get_model("authorization", "VehicleRescueGuidesConfiguration")
    configuration.objects.get_or_create(
        singleton=True,
        defaults={
            "id": uuid.uuid4(),
            "provider": "euro_rescue",
            "mode": "web",
            "web_url": DEFAULT_WEB_URL,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("authorization", "0005_reconcile_lifecycle_provenance_constraint_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="VehicleRescueGuidesConfiguration",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
                ("provider", models.CharField(choices=[("euro_rescue", "Euro RESCUE")], default="euro_rescue", max_length=32)),
                ("mode", models.CharField(choices=[("web", "Web")], default="web", max_length=16)),
                ("web_url", models.CharField(default=DEFAULT_WEB_URL, max_length=2048, validators=[apps.authorization.models.validate_vehicle_rescue_guides_web_url])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="updated_vehicle_rescue_guides_configurations", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(model_name="vehiclerescueguidesconfiguration", constraint=models.CheckConstraint(condition=models.Q(("singleton", True)), name="vehicle_rescue_guides_singleton")),
        migrations.AddConstraint(model_name="vehiclerescueguidesconfiguration", constraint=models.CheckConstraint(condition=models.Q(("provider", "euro_rescue")), name="vehicle_rescue_guides_provider")),
        migrations.AddConstraint(model_name="vehiclerescueguidesconfiguration", constraint=models.CheckConstraint(condition=models.Q(("mode", "web")), name="vehicle_rescue_guides_mode")),
        migrations.RunPython(create_default_configuration, migrations.RunPython.noop),
    ]
