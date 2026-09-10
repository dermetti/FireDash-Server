"""Read-only, department-scoped outbound-mail delivery readiness resolution."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.application_secrets.crypto import ApplicationSecretError, decrypt_application_secret
from apps.authorization.services import is_system_managed_mail_allowed
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration, SystemMailConfiguration
from apps.outbound_mail.providers import SMTP, resolve_effective_provider
from apps.outbound_mail.runtime import MailProvider, VerificationOutcome
from apps.outbound_mail.services import department_smtp_password_context


class OutboundMailUnavailableReason:
    """Stable, sanitized reasons why a department cannot currently deliver mail."""

    DEPARTMENT_DISABLED = "department_disabled"
    MANAGED_MAIL_NOT_ALLOWED = "managed_mail_not_allowed"
    SYSTEM_MAIL_DISABLED = "system_mail_disabled"
    SYSTEM_CONFIGURATION_INCOMPLETE = "system_configuration_incomplete"
    SYSTEM_VERIFICATION_REQUIRED = "system_verification_required"
    DEPARTMENT_SMTP_CONFIGURATION_INCOMPLETE = "department_smtp_configuration_incomplete"
    DEPARTMENT_SMTP_VERIFICATION_REQUIRED = "department_smtp_verification_required"
    PROVIDER_CONFIGURATION = "provider_configuration"


@dataclass(frozen=True, repr=False)
class DepartmentMailProviderResolution:
    """A provider-neutral usable provider, or a safe unavailable reason.

    The provider is deliberately omitted from representations because an adapter can
    hold decrypted credentials.  Consumers must check ``is_usable`` rather than
    interpreting configuration rows or verification state themselves.
    """

    provider: MailProvider | None
    provider_id: str = ""
    unavailable_reason: str = ""

    @property
    def is_usable(self) -> bool:
        return self.provider is not None

    def __repr__(self) -> str:
        if self.is_usable:
            return (
                f"DepartmentMailProviderResolution(usable=True, provider_id={self.provider_id!r})"
            )
        return (
            "DepartmentMailProviderResolution(usable=False, "
            f"unavailable_reason={self.unavailable_reason!r})"
        )

    __str__ = __repr__


def _unavailable(reason: str) -> DepartmentMailProviderResolution:
    return DepartmentMailProviderResolution(provider=None, unavailable_reason=reason)


def _is_current_system_verification(
    *, configuration: SystemMailConfiguration, provider_id: str
) -> bool:
    return bool(
        configuration.verification_provider == provider_id
        and configuration.last_verification_outcome == VerificationOutcome.SUCCESS
        and configuration.last_verified_at is not None
    )


def _is_current_department_smtp_verification(*, configuration: DepartmentMailConfiguration) -> bool:
    return bool(
        configuration.last_smtp_verification_outcome == VerificationOutcome.SUCCESS
        and configuration.last_smtp_verified_at is not None
    )


def _resolve_system_provider() -> DepartmentMailProviderResolution:
    """Resolve only an already-verified active system provider."""
    configuration = SystemMailConfiguration.objects.filter(singleton=True).first()
    if (
        configuration is None
        or configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.DISABLED
    ):
        return _unavailable(OutboundMailUnavailableReason.SYSTEM_MAIL_DISABLED)

    provider_id = (
        configuration.api_provider
        if configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.API
        else SMTP
        if configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.SMTP
        else ""
    )
    try:
        configuration.validate_activation()
    except ValidationError:
        return _unavailable(OutboundMailUnavailableReason.SYSTEM_CONFIGURATION_INCOMPLETE)
    if not _is_current_system_verification(configuration=configuration, provider_id=provider_id):
        return _unavailable(OutboundMailUnavailableReason.SYSTEM_VERIFICATION_REQUIRED)
    try:
        provider = resolve_effective_provider(
            delivery_mode=configuration.delivery_mode, api_provider=configuration.api_provider
        )
    except Exception:
        # Provider factories are extension points.  Their construction failure must
        # never turn a readiness query into a raw adapter/configuration exception.
        return _unavailable(OutboundMailUnavailableReason.PROVIDER_CONFIGURATION)
    return DepartmentMailProviderResolution(provider=provider, provider_id=provider.provider_id)


def _resolve_department_smtp_provider(
    *, configuration: DepartmentMailConfiguration, department: Department
) -> DepartmentMailProviderResolution:
    try:
        configuration.validate_smtp_configuration()
        if bool(configuration.smtp_username) != bool(configuration.smtp_password_encrypted):
            raise ValueError
    except (ValidationError, ValueError):
        return _unavailable(OutboundMailUnavailableReason.DEPARTMENT_SMTP_CONFIGURATION_INCOMPLETE)
    if not _is_current_department_smtp_verification(configuration=configuration):
        return _unavailable(OutboundMailUnavailableReason.DEPARTMENT_SMTP_VERIFICATION_REQUIRED)

    # Construct the existing generic SMTP adapter only after all readiness gates.
    # This is the sole department credential-decryption path and has no I/O.
    try:
        from apps.outbound_mail.smtp import SmtpEffectiveConfiguration, SmtpProvider

        password = ""
        if configuration.smtp_username:
            password = decrypt_application_secret(
                configuration.smtp_password_encrypted,
                context=department_smtp_password_context(department=department),
            ).decode("utf-8")
        provider = SmtpProvider(
            configuration=SmtpEffectiveConfiguration(
                host=configuration.smtp_host,
                port=configuration.smtp_port,
                tls_mode=configuration.smtp_tls_mode,
                sender_name=configuration.smtp_sender_name,
                sender_email=configuration.smtp_sender_email,
                username=configuration.smtp_username,
                password=password,
                timeout=settings.OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS,
            )
        )
    except (ApplicationSecretError, UnicodeDecodeError, ValueError):
        return _unavailable(OutboundMailUnavailableReason.PROVIDER_CONFIGURATION)
    return DepartmentMailProviderResolution(provider=provider, provider_id=provider.provider_id)


def resolve_department_mail_provider(*, department: Department) -> DepartmentMailProviderResolution:
    """Return the department's current effective provider without side effects or I/O."""
    configuration = DepartmentMailConfiguration.objects.filter(department=department).first()
    if (
        configuration is None
        or configuration.delivery_mode == DepartmentMailConfiguration.DeliveryMode.DISABLED
    ):
        return _unavailable(OutboundMailUnavailableReason.DEPARTMENT_DISABLED)
    if configuration.delivery_mode == DepartmentMailConfiguration.DeliveryMode.SYSTEM:
        if not is_system_managed_mail_allowed(department=department):
            return _unavailable(OutboundMailUnavailableReason.MANAGED_MAIL_NOT_ALLOWED)
        return _resolve_system_provider()
    if configuration.delivery_mode == DepartmentMailConfiguration.DeliveryMode.CUSTOM_SMTP:
        return _resolve_department_smtp_provider(configuration=configuration, department=department)
    return _unavailable(OutboundMailUnavailableReason.DEPARTMENT_DISABLED)
