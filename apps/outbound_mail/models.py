import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.outbound_mail.providers import BREVO, validate_api_provider_configuration

validate_sender_email = EmailValidator(message="A valid sender email address is required.")


class SystemMailConfiguration(models.Model):
    """The one system-owned mail configuration; encrypted fields are opaque envelopes."""

    class DeliveryMode(models.TextChoices):
        DISABLED = "DISABLED", "Disabled"
        API = "API", "API"
        SMTP = "SMTP", "SMTP"

    class ApiProvider(models.TextChoices):
        BREVO = BREVO, "Brevo"

    class SmtpTlsMode(models.TextChoices):
        STARTTLS = "STARTTLS", "STARTTLS"
        IMPLICIT_TLS = "IMPLICIT_TLS", "Implicit TLS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    singleton = models.BooleanField(default=True, unique=True, editable=False)
    delivery_mode = models.CharField(
        max_length=16, choices=DeliveryMode.choices, default=DeliveryMode.DISABLED
    )
    api_provider = models.CharField(
        max_length=32, choices=ApiProvider.choices, blank=True, default=""
    )
    brevo_sender_name = models.CharField(max_length=255, blank=True, default="")
    brevo_sender_email = models.EmailField(
        blank=True, default="", validators=[validate_sender_email]
    )
    brevo_api_key_encrypted = models.CharField(
        max_length=4096, blank=True, default="", editable=False
    )
    smtp_host = models.CharField(max_length=255, blank=True, default="")
    smtp_port = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    smtp_tls_mode = models.CharField(
        max_length=16, choices=SmtpTlsMode.choices, blank=True, default=""
    )
    smtp_sender_name = models.CharField(max_length=255, blank=True, default="")
    smtp_sender_email = models.EmailField(
        blank=True, default="", validators=[validate_sender_email]
    )
    smtp_username = models.CharField(max_length=255, blank=True, default="")
    smtp_password_encrypted = models.CharField(
        max_length=4096, blank=True, default="", editable=False
    )
    verification_provider = models.CharField(max_length=32, blank=True, default="", editable=False)
    last_verification_outcome = models.CharField(
        max_length=16, blank=True, default="", editable=False
    )
    last_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_verification_code = models.CharField(max_length=64, blank=True, default="", editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_system_mail_configurations",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(singleton=True), name="system_mail_configuration_singleton"
            ),
            models.CheckConstraint(
                condition=Q(smtp_username="", smtp_password_encrypted="")
                | (Q(smtp_username__gt="") & Q(smtp_password_encrypted__gt="")),
                name="system_mail_smtp_auth_pair",
            ),
        ]

    def __str__(self) -> str:
        return f"System mail configuration ({self.delivery_mode})"

    @property
    def brevo_api_key_configured(self) -> bool:
        return bool(self.brevo_api_key_encrypted)

    @property
    def smtp_password_configured(self) -> bool:
        return bool(self.smtp_password_encrypted)

    def validate_activation(self) -> None:
        if self.delivery_mode == self.DeliveryMode.DISABLED:
            return
        if self.delivery_mode == self.DeliveryMode.API:
            if not self.api_provider:
                raise ValidationError("An API provider is required for API mail delivery.")
            validate_api_provider_configuration(provider=self.api_provider, configuration=self)
            return
        if self.delivery_mode == self.DeliveryMode.SMTP:
            self.validate_smtp_configuration()
            if bool(self.smtp_username) != bool(self.smtp_password_encrypted):
                raise ValidationError("SMTP username and password must be configured together.")
            return
        raise ValidationError("Unsupported mail delivery mode.")

    def validate_brevo_sender_configuration(self) -> None:
        if not self.brevo_sender_name or not self.brevo_sender_email:
            raise ValidationError("Brevo sender name and email are required.")

    def validate_smtp_configuration(self) -> None:
        required = (
            self.smtp_host,
            self.smtp_port,
            self.smtp_tls_mode,
            self.smtp_sender_name,
            self.smtp_sender_email,
        )
        if not all(required):
            raise ValidationError("SMTP delivery configuration is incomplete.")
