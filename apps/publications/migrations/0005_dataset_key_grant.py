import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publications", "0004_phase7_closeout_integrity"),
        ("tablets", "0002_tablet_asset_number_tablet_created_by_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="DatasetKeyGrant",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("READY", "Ready"),
                            ("FAILED", "Failed"),
                            ("REVOKED", "Revoked"),
                        ],
                        default="PENDING",
                        max_length=12,
                    ),
                ),
                ("hpke_ciphersuite", models.CharField(blank=True, max_length=128)),
                ("hpke_encapsulated_key", models.BinaryField(blank=True, null=True)),
                ("hpke_wrapped_content_key", models.BinaryField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.CharField(blank=True, max_length=512)),
                (
                    "app_installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dataset_key_grants",
                        to="tablets.appinstallation",
                    ),
                ),
                (
                    "publication",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="key_grants",
                        to="publications.datasetpublication",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="datasetkeygrant",
            constraint=models.UniqueConstraint(
                fields=("publication", "app_installation"), name="unique_dataset_key_grant"
            ),
        ),
        migrations.AddIndex(
            model_name="datasetkeygrant",
            index=models.Index(
                fields=["status", "created_at"], name="key_grant_status_created_idx"
            ),
        ),
    ]
