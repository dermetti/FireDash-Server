import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tablets.versions


class Migration(migrations.Migration):
    dependencies = [
        ("authorization", "0002_single_department_admin_constraint"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiVersionCompatibilityPolicy",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("api_major", models.PositiveSmallIntegerField(unique=True)),
                (
                    "minimum_app_version",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        null=True,
                        validators=[apps.tablets.versions.validate_app_version],
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="updated_api_version_compatibility_policies",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="apiversioncompatibilitypolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("api_major__gt", 0)), name="api_policy_positive_major"
            ),
        ),
    ]
