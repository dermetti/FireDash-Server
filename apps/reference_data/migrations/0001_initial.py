import uuid

import django.db.models.deletion
from django.conf import settings
from django.contrib.gis.db.models import fields as gis_fields
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0002_vehicle"),
    ]

    operations = [
        migrations.CreateModel(
            name="FirePlan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("object_name", models.CharField(max_length=255)),
                ("object_reference", models.CharField(blank=True, max_length=255)),
                ("address", models.TextField(blank=True)),
                ("location", gis_fields.PointField(blank=True, null=True, srid=4326)),
                ("document_key", models.CharField(max_length=255, unique=True)),
                ("original_filename", models.CharField(max_length=255)),
                ("file_size", models.PositiveBigIntegerField()),
                ("page_count", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fire_plans",
                        to="organizations.department",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_fire_plans",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Hydrant",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("external_identifier", models.CharField(blank=True, max_length=255)),
                ("location", gis_fields.PointField(srid=4326)),
                ("hydrant_type", models.CharField(blank=True, max_length=128)),
                ("flow_information", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(blank=True, max_length=128)),
                ("source_metadata", models.JSONField(default=dict)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="hydrants",
                        to="organizations.department",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HydrantImportPreview",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("normalized_features", models.JSONField()),
                ("duplicate_count", models.PositiveIntegerField(default=0)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="organizations.department"
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="fireplan",
            index=models.Index(
                fields=["department", "active"], name="reference_d_departm_3ca664_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="hydrant",
            index=models.Index(
                fields=["department", "active"], name="reference_d_departm_e48c4a_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="hydrant",
            constraint=models.UniqueConstraint(
                condition=~models.Q(("external_identifier", "")),
                fields=("department", "external_identifier"),
                name="unique_hydrant_external_identifier_per_department",
            ),
        ),
    ]
