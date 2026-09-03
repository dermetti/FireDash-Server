import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def add_dangerous_goods_registry_entry(apps, schema_editor):
    registry = apps.get_model("publications", "DatasetTypeRegistry")
    registry.objects.update_or_create(
        code="dangerous_goods",
        defaults={
            "scope": "department",
            "current_schema_version": 1,
            "supported_schema_versions": [1],
            "required": True,
            "feature_code": "publications",
        },
    )


class Migration(migrations.Migration):
    dependencies = [("publications", "0023_phonebook_dataset_scope_registry")]

    operations = [
        migrations.CreateModel(
            name="DatasetSourceRevision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("source_revision", models.PositiveBigIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("byte_size", models.PositiveBigIntegerField()),
                ("import_summary", models.JSONField(default=dict)),
                ("plaintext", models.BinaryField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_dataset_source_revisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "scope_state",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_revisions",
                        to="publications.datasetscopestate",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="datasetsourcerevision",
            constraint=models.UniqueConstraint(
                fields=("scope_state", "source_revision"), name="unique_dataset_source_revision"
            ),
        ),
        migrations.AddIndex(
            model_name="datasetsourcerevision",
            index=models.Index(
                fields=["scope_state", "-source_revision"], name="publications_scope_s_631ca6_idx"
            ),
        ),
        migrations.RunPython(add_dangerous_goods_registry_entry, migrations.RunPython.noop),
    ]
