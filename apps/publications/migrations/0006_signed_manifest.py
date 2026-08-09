import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publications", "0005_dataset_key_grant"),
    ]

    operations = [
        migrations.CreateModel(
            name="SignedManifest",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("state_hash", models.CharField(max_length=64)),
                ("generation", models.PositiveIntegerField(default=1)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("READY", "Ready"),
                            ("FAILED", "Failed"),
                            ("OBSOLETE", "Obsolete"),
                        ],
                        default="PENDING",
                        max_length=12,
                    ),
                ),
                ("payload", models.JSONField(default=dict)),
                ("signature", models.BinaryField(blank=True, null=True)),
                ("signature_algorithm", models.CharField(blank=True, max_length=32)),
                ("signing_key_version", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.CharField(blank=True, max_length=512)),
                (
                    "app_installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="signed_manifests",
                        to="tablets.appinstallation",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="signedmanifest",
            constraint=models.UniqueConstraint(
                fields=("app_installation", "state_hash"), name="unique_signed_manifest_state"
            ),
        ),
        migrations.AddIndex(
            model_name="signedmanifest",
            index=models.Index(fields=["status", "created_at"], name="signed_manifest_status_idx"),
        ),
    ]
