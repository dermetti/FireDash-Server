import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("outbound_mail", "0004_departmentmailconfiguration_last_smtp_verification_code_and_more"),
        ("tablets", "0008_remove_obsolete_reactivation_flow"),
    ]

    operations = [
        migrations.CreateModel(
            name="TabletReportDeliveryRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("delivery_request_id", models.UUIDField()),
                ("recipient_personnel_id", models.UUIDField()),
                ("state", models.CharField(choices=[("PROCESSING", "Processing"), ("SUCCESS", "Success"), ("FAILED", "Failed"), ("UNKNOWN", "Unknown")], default="PROCESSING", max_length=16)),
                ("result_code", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="organizations.department")),
                ("installation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="report_delivery_requests", to="tablets.appinstallation")),
            ],
        ),
        migrations.AddConstraint(
            model_name="tabletreportdeliveryrequest",
            constraint=models.UniqueConstraint(fields=("installation", "delivery_request_id"), name="unique_tablet_report_delivery_request"),
        ),
    ]
