import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_default_system_mail_configuration(apps, schema_editor):
    configuration = apps.get_model("outbound_mail", "SystemMailConfiguration")
    configuration.objects.get_or_create(singleton=True, defaults={"delivery_mode": "DISABLED"})


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SystemMailConfiguration",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
                (
                    "delivery_mode",
                    models.CharField(
                        choices=[("DISABLED", "Disabled"), ("API", "API"), ("SMTP", "SMTP")],
                        default="DISABLED",
                        max_length=16,
                    ),
                ),
                (
                    "api_provider",
                    models.CharField(
                        blank=True, choices=[("BREVO", "Brevo")], default="", max_length=32
                    ),
                ),
                ("brevo_sender_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "brevo_sender_email",
                    models.EmailField(
                        blank=True,
                        default="",
                        max_length=254,
                        validators=[
                            django.core.validators.EmailValidator(
                                message="A valid sender email address is required."
                            )
                        ],
                    ),
                ),
                (
                    "brevo_api_key_encrypted",
                    models.CharField(blank=True, default="", editable=False, max_length=4096),
                ),
                ("smtp_host", models.CharField(blank=True, default="", max_length=255)),
                (
                    "smtp_port",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(65535),
                        ],
                    ),
                ),
                (
                    "smtp_tls_mode",
                    models.CharField(
                        blank=True,
                        choices=[("STARTTLS", "STARTTLS"), ("IMPLICIT_TLS", "Implicit TLS")],
                        default="",
                        max_length=16,
                    ),
                ),
                ("smtp_sender_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "smtp_sender_email",
                    models.EmailField(
                        blank=True,
                        default="",
                        max_length=254,
                        validators=[
                            django.core.validators.EmailValidator(
                                message="A valid sender email address is required."
                            )
                        ],
                    ),
                ),
                ("smtp_username", models.CharField(blank=True, default="", max_length=255)),
                (
                    "smtp_password_encrypted",
                    models.CharField(blank=True, default="", editable=False, max_length=4096),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="updated_system_mail_configurations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("singleton", True)),
                        name="system_mail_configuration_singleton",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("smtp_password_encrypted", ""), ("smtp_username", "")),
                            models.Q(
                                ("smtp_username__gt", ""), ("smtp_password_encrypted__gt", "")
                            ),
                            _connector="OR",
                        ),
                        name="system_mail_smtp_auth_pair",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_default_system_mail_configuration, migrations.RunPython.noop),
    ]
