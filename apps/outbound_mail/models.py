import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.organizations.models import Department
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
    # ``None`` deliberately means use the deployment environment default.  An
    # empty string is an explicit System Admin choice to use direct HTTPS.
    outbound_mail_https_proxy = models.CharField(
        max_length=2048, null=True, blank=True, default=None
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


class DepartmentMailConfiguration(models.Model):
    """Optional department mail state; absent rows deliberately mean disabled."""

    class DeliveryMode(models.TextChoices):
        DISABLED = "DISABLED", "Disabled"
        SYSTEM = "SYSTEM", "System managed"
        CUSTOM_SMTP = "CUSTOM_SMTP", "Custom SMTP"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.OneToOneField(
        Department, on_delete=models.PROTECT, related_name="mail_configuration"
    )
    delivery_mode = models.CharField(
        max_length=16, choices=DeliveryMode.choices, default=DeliveryMode.DISABLED
    )
    smtp_host = models.CharField(max_length=255, blank=True, default="")
    smtp_port = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    smtp_tls_mode = models.CharField(
        max_length=16, choices=SystemMailConfiguration.SmtpTlsMode.choices, blank=True, default=""
    )
    smtp_sender_name = models.CharField(max_length=255, blank=True, default="")
    smtp_sender_email = models.EmailField(
        blank=True, default="", validators=[validate_sender_email]
    )
    smtp_username = models.CharField(max_length=255, blank=True, default="")
    smtp_password_encrypted = models.CharField(
        max_length=4096, blank=True, default="", editable=False
    )
    last_smtp_verification_outcome = models.CharField(
        max_length=16, blank=True, default="", editable=False
    )
    last_smtp_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_smtp_verification_code = models.CharField(
        max_length=64, blank=True, default="", editable=False
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_department_mail_configurations",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(smtp_username="", smtp_password_encrypted="")
                | (Q(smtp_username__gt="") & Q(smtp_password_encrypted__gt="")),
                name="department_mail_smtp_auth_pair",
            ),
        ]

    def __str__(self) -> str:
        return f"Department mail configuration ({self.delivery_mode})"

    @property
    def smtp_password_configured(self) -> bool:
        return bool(self.smtp_password_encrypted)

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


class DepartmentRecipientPolicy(models.Model):
    """Optional exact-domain restriction for a department's future mail recipients."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.OneToOneField(
        Department, on_delete=models.PROTECT, related_name="mail_recipient_policy"
    )
    restriction_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_department_mail_recipient_policies",
    )

    def __str__(self) -> str:
        return "Department mail recipient policy"


class DepartmentRecipientDomain(models.Model):
    """A canonical exact recipient domain, never a suffix or wildcard pattern."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        DepartmentRecipientPolicy, on_delete=models.CASCADE, related_name="approved_domains"
    )
    domain = models.CharField(max_length=253)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("policy", "domain"), name="unique_department_mail_domain"
            )
        ]

    def __str__(self) -> str:
        return self.domain


class TabletReportDeliveryRequest(models.Model):
    """Minimal durable idempotency state; report content is never stored."""

    class State(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        UNKNOWN = "UNKNOWN", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(
        "tablets.AppInstallation", on_delete=models.PROTECT, related_name="report_delivery_requests"
    )
    department = models.ForeignKey(Department, on_delete=models.PROTECT)
    delivery_request_id = models.UUIDField()
    recipient_personnel_id = models.UUIDField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.PROCESSING)
    result_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("installation", "delivery_request_id"),
                name="unique_tablet_report_delivery_request",
            )
        ]

    def __str__(self) -> str:
        return f"Tablet report delivery request ({self.state})"
