"""Audited mutation and safe query services for system outbound-mail settings."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.application_secrets.crypto import (
    ApplicationSecretError,
    decrypt_application_secret,
    encrypt_application_secret,
)
from apps.audit.services import record_event
from apps.authorization.scopes import is_system_admin
from apps.authorization.services import (
    is_system_managed_mail_allowed,
    require_department_admin,
    require_system_admin,
)
from apps.organizations.models import Department
from apps.outbound_mail.models import (
    DepartmentMailConfiguration,
    DepartmentRecipientDomain,
    DepartmentRecipientPolicy,
    SystemMailConfiguration,
)
from apps.outbound_mail.providers import BREVO, resolve_effective_provider
from apps.outbound_mail.runtime import (
    MailProviderError,
    ProviderConfigurationError,
    VerificationOutcome,
)

BREVO_API_KEY_CONTEXT = "outbound-mail:api:brevo"
SMTP_PASSWORD_CONTEXT = "outbound-mail:smtp:password"
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class SystemMailConfigurationState:
    delivery_mode: str
    api_provider: str
    brevo_sender_name: str
    brevo_sender_email: str
    brevo_api_key_configured: bool
    smtp_host: str
    smtp_port: int | None
    smtp_tls_mode: str
    smtp_sender_name: str
    smtp_sender_email: str
    smtp_username_configured: bool
    smtp_password_configured: bool
    verification_provider: str
    last_verification_outcome: str
    last_verified_at: datetime | None
    last_verification_code: str
    outbound_mail_https_proxy: str | None
    outbound_mail_https_proxy_configured: bool
    outbound_mail_https_proxy_uses_deployment_default: bool

    @property
    def brevo_configuration_complete(self) -> bool:
        """Safe readiness projection; credentials remain opaque."""
        return bool(
            self.brevo_sender_name and self.brevo_sender_email and self.brevo_api_key_configured
        )

    @property
    def smtp_configuration_complete(self) -> bool:
        """SMTP authentication is optional, but never partially configured."""
        return bool(
            self.smtp_host
            and self.smtp_port
            and self.smtp_tls_mode
            and self.smtp_sender_name
            and self.smtp_sender_email
            and self.smtp_username_configured == self.smtp_password_configured
        )


def _configuration() -> SystemMailConfiguration:
    try:
        configuration, _ = SystemMailConfiguration.objects.get_or_create(singleton=True)
    except IntegrityError:
        configuration = SystemMailConfiguration.objects.get(singleton=True)
    return configuration


def get_system_mail_configuration() -> SystemMailConfigurationState:
    configuration = _configuration()
    proxy_uses_deployment_default = configuration.outbound_mail_https_proxy is None
    effective_proxy = (
        settings.OUTBOUND_MAIL_HTTPS_PROXY
        if proxy_uses_deployment_default
        else configuration.outbound_mail_https_proxy
    )
    return SystemMailConfigurationState(
        delivery_mode=configuration.delivery_mode,
        api_provider=configuration.api_provider,
        brevo_sender_name=configuration.brevo_sender_name,
        brevo_sender_email=configuration.brevo_sender_email,
        brevo_api_key_configured=configuration.brevo_api_key_configured,
        smtp_host=configuration.smtp_host,
        smtp_port=configuration.smtp_port,
        smtp_tls_mode=configuration.smtp_tls_mode,
        smtp_sender_name=configuration.smtp_sender_name,
        smtp_sender_email=configuration.smtp_sender_email,
        smtp_username_configured=bool(configuration.smtp_username),
        smtp_password_configured=configuration.smtp_password_configured,
        verification_provider=configuration.verification_provider,
        last_verification_outcome=configuration.last_verification_outcome,
        last_verified_at=configuration.last_verified_at,
        last_verification_code=configuration.last_verification_code,
        outbound_mail_https_proxy=effective_proxy,
        outbound_mail_https_proxy_configured=bool(effective_proxy),
        outbound_mail_https_proxy_uses_deployment_default=proxy_uses_deployment_default,
    )


def _locked_configuration() -> SystemMailConfiguration:
    _configuration()
    return SystemMailConfiguration.objects.select_for_update().get(singleton=True)


def _audit(
    *, actor, action: str, configuration: SystemMailConfiguration, metadata: dict[str, str | bool]
) -> None:
    record_event(
        action=action,
        actor_user=actor,
        target_type="system_mail_configuration",
        target_uuid=configuration.id,
        metadata=metadata,
    )


def _invalidate_verification(configuration: SystemMailConfiguration) -> None:
    configuration.verification_provider = ""
    configuration.last_verification_outcome = ""
    configuration.last_verified_at = None
    configuration.last_verification_code = ""


@transaction.atomic
def configure_outbound_mail_https_proxy(*, actor, proxy_url: str) -> SystemMailConfiguration:
    """Set the system-owned HTTPS egress route without exposing it to providers."""
    require_system_admin(actor)
    from apps.outbound_mail.http_transport import validate_outbound_mail_https_proxy

    proxy_url = proxy_url.strip()
    validate_outbound_mail_https_proxy(proxy_url)
    configuration = _locked_configuration()
    if configuration.outbound_mail_https_proxy == proxy_url:
        return configuration
    configuration.outbound_mail_https_proxy = proxy_url
    configuration.updated_by = actor
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "outbound_mail_https_proxy",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.https_proxy_changed",
        configuration=configuration,
        metadata={"configured": bool(proxy_url)},
    )
    return configuration


@dataclass(frozen=True)
class ProviderVerificationResult:
    provider: str
    outcome: str
    verified_at: datetime
    diagnostic_code: str


@transaction.atomic
def verify_system_mail_configuration(*, actor) -> ProviderVerificationResult:
    """Observe the selected provider without sending or changing its activation."""
    require_system_admin(actor)
    configuration = _locked_configuration()
    provider_identity = (
        configuration.api_provider
        if configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.API
        else "SMTP"
        if configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.SMTP
        else ""
    )
    verified_at = timezone.now()
    try:
        provider = resolve_effective_provider(
            delivery_mode=configuration.delivery_mode, api_provider=configuration.api_provider
        )
        verify = getattr(provider, "verify", None)
        if not callable(verify):
            raise ProviderConfigurationError()
        verify()
    except MailProviderError as error:
        outcome = VerificationOutcome.FAILED
        diagnostic_code = error.code
    else:
        outcome = VerificationOutcome.SUCCESS
        diagnostic_code = "verified"
        provider_identity = provider.provider_id
    configuration.verification_provider = provider_identity
    configuration.last_verification_outcome = outcome
    configuration.last_verified_at = verified_at
    configuration.last_verification_code = diagnostic_code
    configuration.save(
        update_fields=(
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.provider_verification_completed",
        configuration=configuration,
        metadata={"provider": provider_identity, "outcome": outcome, "code": diagnostic_code},
    )
    return ProviderVerificationResult(
        provider=provider_identity,
        outcome=outcome,
        verified_at=verified_at,
        diagnostic_code=diagnostic_code,
    )


@transaction.atomic
def configure_brevo(*, actor, sender_name: str, sender_email: str) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    configuration.brevo_sender_name = sender_name.strip()
    configuration.brevo_sender_email = sender_email.strip()
    configuration.updated_by = actor
    configuration.full_clean(exclude=("brevo_api_key_encrypted", "smtp_password_encrypted"))
    configuration.validate_brevo_sender_configuration()
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "brevo_sender_name",
            "brevo_sender_email",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.brevo_configuration_changed",
        configuration=configuration,
        metadata={"provider": BREVO},
    )
    return configuration


@transaction.atomic
def replace_brevo_api_key(*, actor, api_key: str) -> SystemMailConfiguration:
    require_system_admin(actor)
    if not api_key:
        raise ValidationError("An API key is required for replacement.")
    configuration = _locked_configuration()
    configuration.brevo_api_key_encrypted = encrypt_application_secret(
        api_key, context=BREVO_API_KEY_CONTEXT
    ).serialized
    configuration.updated_by = actor
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "brevo_api_key_encrypted",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.brevo_api_key_replaced",
        configuration=configuration,
        metadata={"provider": BREVO, "configured": True},
    )
    return configuration


@transaction.atomic
def clear_brevo_api_key(*, actor) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    configuration.brevo_api_key_encrypted = ""
    configuration.updated_by = actor
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "brevo_api_key_encrypted",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.brevo_api_key_cleared",
        configuration=configuration,
        metadata={"provider": BREVO, "configured": False},
    )
    return configuration


@transaction.atomic
def configure_smtp(
    *, actor, host: str, port: int, tls_mode: str, sender_name: str, sender_email: str
) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    configuration.smtp_host = host.strip()
    configuration.smtp_port = port
    configuration.smtp_tls_mode = tls_mode
    configuration.smtp_sender_name = sender_name.strip()
    configuration.smtp_sender_email = sender_email.strip()
    configuration.updated_by = actor
    configuration.full_clean(exclude=("brevo_api_key_encrypted", "smtp_password_encrypted"))
    configuration.validate_smtp_configuration()
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "smtp_host",
            "smtp_port",
            "smtp_tls_mode",
            "smtp_sender_name",
            "smtp_sender_email",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.smtp_configuration_changed",
        configuration=configuration,
        metadata={"tls_mode": tls_mode},
    )
    return configuration


@transaction.atomic
def replace_smtp_credentials(*, actor, username: str, password: str) -> SystemMailConfiguration:
    require_system_admin(actor)
    if not username or not password:
        raise ValidationError("SMTP username and password are both required for replacement.")
    configuration = _locked_configuration()
    configuration.smtp_username = username
    configuration.smtp_password_encrypted = encrypt_application_secret(
        password, context=SMTP_PASSWORD_CONTEXT
    ).serialized
    configuration.updated_by = actor
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "smtp_username",
            "smtp_password_encrypted",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.smtp_credentials_replaced",
        configuration=configuration,
        metadata={"configured": True},
    )
    return configuration


@transaction.atomic
def clear_smtp_credentials(*, actor) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    configuration.smtp_username = ""
    configuration.smtp_password_encrypted = ""
    configuration.updated_by = actor
    _invalidate_verification(configuration)
    configuration.save(
        update_fields=(
            "smtp_username",
            "smtp_password_encrypted",
            "updated_by",
            "updated_at",
            "verification_provider",
            "last_verification_outcome",
            "last_verified_at",
            "last_verification_code",
        )
    )
    _audit(
        actor=actor,
        action="outbound_mail.smtp_credentials_cleared",
        configuration=configuration,
        metadata={"configured": False},
    )
    return configuration


@transaction.atomic
def set_delivery_mode(
    *, actor, delivery_mode: str, api_provider: str = ""
) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    old_mode, old_provider = configuration.delivery_mode, configuration.api_provider
    configuration.delivery_mode = delivery_mode
    if delivery_mode == SystemMailConfiguration.DeliveryMode.API:
        configuration.api_provider = api_provider
    configuration.full_clean(exclude=("brevo_api_key_encrypted", "smtp_password_encrypted"))
    configuration.validate_activation()
    configuration.updated_by = actor
    configuration.save(update_fields=("delivery_mode", "api_provider", "updated_by", "updated_at"))
    _audit(
        actor=actor,
        action="outbound_mail.delivery_mode_changed",
        configuration=configuration,
        metadata={
            "old_mode": old_mode,
            "new_mode": delivery_mode,
            "old_provider": old_provider,
            "new_provider": configuration.api_provider,
        },
    )
    return configuration


@dataclass(frozen=True)
class DepartmentMailConfigurationState:
    """Safe presentation/query projection; credential envelopes never leave the model boundary."""

    delivery_mode: str
    smtp_host: str
    smtp_port: int | None
    smtp_tls_mode: str
    smtp_sender_name: str
    smtp_sender_email: str
    smtp_username: str
    smtp_username_configured: bool
    smtp_password_configured: bool
    last_smtp_verification_outcome: str
    last_smtp_verified_at: datetime | None
    last_smtp_verification_code: str


def department_smtp_password_context(*, department: Department) -> str:
    """Bind a department SMTP password to this tenant and this credential purpose."""
    return f"outbound-mail:department:{department.id}:smtp:password"


def _require_department_mail_manager(*, actor, department: Department) -> None:
    if not is_system_admin(actor):
        require_department_admin(actor, department)


def _department_configuration(*, department: Department) -> DepartmentMailConfiguration | None:
    return DepartmentMailConfiguration.objects.filter(department=department).first()


def get_department_mail_configuration(
    *, department: Department
) -> DepartmentMailConfigurationState:
    configuration = _department_configuration(department=department)
    if configuration is None:
        return DepartmentMailConfigurationState(
            delivery_mode=DepartmentMailConfiguration.DeliveryMode.DISABLED,
            smtp_host="",
            smtp_port=None,
            smtp_tls_mode="",
            smtp_sender_name="",
            smtp_sender_email="",
            smtp_username="",
            smtp_username_configured=False,
            smtp_password_configured=False,
            last_smtp_verification_outcome="",
            last_smtp_verified_at=None,
            last_smtp_verification_code="",
        )
    return DepartmentMailConfigurationState(
        delivery_mode=configuration.delivery_mode,
        smtp_host=configuration.smtp_host,
        smtp_port=configuration.smtp_port,
        smtp_tls_mode=configuration.smtp_tls_mode,
        smtp_sender_name=configuration.smtp_sender_name,
        smtp_sender_email=configuration.smtp_sender_email,
        smtp_username=configuration.smtp_username,
        smtp_username_configured=bool(configuration.smtp_username),
        smtp_password_configured=configuration.smtp_password_configured,
        last_smtp_verification_outcome=configuration.last_smtp_verification_outcome,
        last_smtp_verified_at=configuration.last_smtp_verified_at,
        last_smtp_verification_code=configuration.last_smtp_verification_code,
    )


def _locked_department_configuration(*, department: Department) -> DepartmentMailConfiguration:
    configuration, _ = DepartmentMailConfiguration.objects.get_or_create(department=department)
    return DepartmentMailConfiguration.objects.select_for_update().get(pk=configuration.pk)


def _audit_department_configuration(*, actor, action: str, configuration, metadata: dict) -> None:
    record_event(
        action=action,
        actor_user=actor,
        department=configuration.department,
        target_type="department_mail_configuration",
        target_uuid=configuration.id,
        metadata=metadata,
    )


def _invalidate_department_smtp_verification(configuration: DepartmentMailConfiguration) -> None:
    configuration.last_smtp_verification_outcome = ""
    configuration.last_smtp_verified_at = None
    configuration.last_smtp_verification_code = ""


@transaction.atomic
def configure_department_smtp(
    *,
    actor,
    department: Department,
    host: str,
    port: int,
    tls_mode: str,
    sender_name: str,
    sender_email: str,
) -> DepartmentMailConfiguration:
    _require_department_mail_manager(actor=actor, department=department)
    configuration = _locked_department_configuration(department=department)
    updated = {
        "smtp_host": host.strip(),
        "smtp_port": port,
        "smtp_tls_mode": tls_mode,
        "smtp_sender_name": sender_name.strip(),
        "smtp_sender_email": sender_email.strip(),
    }
    candidate_changed = any(getattr(configuration, key) != value for key, value in updated.items())
    configuration.__dict__.update(updated)
    configuration.full_clean(exclude=("smtp_password_encrypted",))
    configuration.validate_smtp_configuration()
    if not candidate_changed:
        return configuration
    configuration.updated_by = actor
    _invalidate_department_smtp_verification(configuration)
    configuration.save(
        update_fields=(
            *updated.keys(),
            "updated_by",
            "updated_at",
            "last_smtp_verification_outcome",
            "last_smtp_verified_at",
            "last_smtp_verification_code",
        )
    )
    _audit_department_configuration(
        actor=actor,
        action="outbound_mail.department_smtp_configuration_changed",
        configuration=configuration,
        metadata={"tls_mode": tls_mode},
    )
    return configuration


@transaction.atomic
def replace_department_smtp_credentials(
    *, actor, department: Department, username: str, password: str
) -> DepartmentMailConfiguration:
    _require_department_mail_manager(actor=actor, department=department)
    if not username or not password:
        raise ValidationError("SMTP username and password are both required for replacement.")
    configuration = _locked_department_configuration(department=department)
    configuration.smtp_username = username
    configuration.smtp_password_encrypted = encrypt_application_secret(
        password, context=department_smtp_password_context(department=department)
    ).serialized
    configuration.updated_by = actor
    _invalidate_department_smtp_verification(configuration)
    configuration.save(
        update_fields=(
            "smtp_username",
            "smtp_password_encrypted",
            "updated_by",
            "updated_at",
            "last_smtp_verification_outcome",
            "last_smtp_verified_at",
            "last_smtp_verification_code",
        )
    )
    _audit_department_configuration(
        actor=actor,
        action="outbound_mail.department_smtp_credentials_replaced",
        configuration=configuration,
        metadata={"configured": True},
    )
    return configuration


@transaction.atomic
def clear_department_smtp_credentials(
    *, actor, department: Department
) -> DepartmentMailConfiguration:
    _require_department_mail_manager(actor=actor, department=department)
    configuration = _locked_department_configuration(department=department)
    if not configuration.smtp_username and not configuration.smtp_password_encrypted:
        return configuration
    configuration.smtp_username = ""
    configuration.smtp_password_encrypted = ""
    configuration.updated_by = actor
    _invalidate_department_smtp_verification(configuration)
    configuration.save(
        update_fields=(
            "smtp_username",
            "smtp_password_encrypted",
            "updated_by",
            "updated_at",
            "last_smtp_verification_outcome",
            "last_smtp_verified_at",
            "last_smtp_verification_code",
        )
    )
    _audit_department_configuration(
        actor=actor,
        action="outbound_mail.department_smtp_credentials_cleared",
        configuration=configuration,
        metadata={"configured": False},
    )
    return configuration


@dataclass(frozen=True)
class DepartmentSmtpVerificationResult:
    outcome: str
    verified_at: datetime
    diagnostic_code: str


@transaction.atomic
def verify_department_smtp_configuration(
    *, actor, department: Department
) -> DepartmentSmtpVerificationResult:
    """Verify a department SMTP endpoint without sending a message or changing mode."""
    _require_department_mail_manager(actor=actor, department=department)
    configuration = (
        DepartmentMailConfiguration.objects.select_for_update()
        .filter(department=department)
        .first()
    )
    if configuration is None:
        raise ValidationError("SMTP delivery configuration is incomplete.")
    try:
        configuration.validate_smtp_configuration()
        if bool(configuration.smtp_username) != bool(configuration.smtp_password_encrypted):
            raise ValueError
        password = ""
        if configuration.smtp_username:
            password = decrypt_application_secret(
                configuration.smtp_password_encrypted,
                context=department_smtp_password_context(department=department),
            ).decode("utf-8")
        # Reuse the Phase 2C generic SMTP provider. It owns TLS, certificate,
        # timeout, authentication, no-message verification, and error mapping.
        from apps.outbound_mail.smtp import SmtpEffectiveConfiguration, SmtpProvider

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
        provider.verify()
    except (ApplicationSecretError, UnicodeDecodeError, ValueError):
        outcome, code = VerificationOutcome.FAILED, "provider_configuration"
    except MailProviderError as error:
        outcome, code = VerificationOutcome.FAILED, error.code
    else:
        outcome, code = VerificationOutcome.SUCCESS, "verified"
    verified_at = timezone.now()
    configuration.last_smtp_verification_outcome = outcome
    configuration.last_smtp_verified_at = verified_at
    configuration.last_smtp_verification_code = code
    configuration.save(
        update_fields=(
            "last_smtp_verification_outcome",
            "last_smtp_verified_at",
            "last_smtp_verification_code",
        )
    )
    _audit_department_configuration(
        actor=actor,
        action="outbound_mail.department_smtp_verification_completed",
        configuration=configuration,
        metadata={"outcome": outcome, "code": code},
    )
    return DepartmentSmtpVerificationResult(
        outcome=outcome, verified_at=verified_at, diagnostic_code=code
    )


@transaction.atomic
def set_department_delivery_mode(
    *, actor, department: Department, delivery_mode: str
) -> DepartmentMailConfiguration:
    _require_department_mail_manager(actor=actor, department=department)
    configuration = _locked_department_configuration(department=department)
    if delivery_mode == DepartmentMailConfiguration.DeliveryMode.SYSTEM:
        if not is_system_managed_mail_allowed(department=department):
            raise ValidationError("This department is not authorized for system-managed mail.")
    elif delivery_mode == DepartmentMailConfiguration.DeliveryMode.CUSTOM_SMTP:
        configuration.validate_smtp_configuration()
        if bool(configuration.smtp_username) != bool(configuration.smtp_password_encrypted):
            raise ValidationError("SMTP username and password must be configured together.")
    elif delivery_mode != DepartmentMailConfiguration.DeliveryMode.DISABLED:
        raise ValidationError("Unsupported department mail delivery mode.")
    if configuration.delivery_mode == delivery_mode:
        return configuration
    old_mode = configuration.delivery_mode
    configuration.delivery_mode = delivery_mode
    configuration.updated_by = actor
    configuration.save(update_fields=("delivery_mode", "updated_by", "updated_at"))
    _audit_department_configuration(
        actor=actor,
        action="outbound_mail.department_delivery_mode_changed",
        configuration=configuration,
        metadata={"old_mode": old_mode, "new_mode": delivery_mode},
    )
    return configuration


def normalize_recipient_domain(domain: str) -> str:
    """Return a canonical, exact DNS name; patterns and IP literals are forbidden."""
    if not isinstance(domain, str) or not domain or domain != domain.strip():
        raise ValidationError("Approved recipient domains must be valid exact domain names.")
    normalized = domain.lower()
    if normalized.startswith((".", "@")) or "*" in normalized or normalized.endswith("."):
        raise ValidationError("Approved recipient domains must be valid exact domain names.")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise ValidationError("Approved recipient domains must be domain names, not IP addresses.")
    labels = normalized.split(".")
    if (
        len(normalized) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL.fullmatch(x) for x in labels)
    ):
        raise ValidationError("Approved recipient domains must be valid exact domain names.")
    return normalized


@dataclass(frozen=True)
class DepartmentRecipientPolicyState:
    restriction_enabled: bool
    approved_domains: tuple[str, ...]


def get_department_recipient_policy(*, department: Department) -> DepartmentRecipientPolicyState:
    policy = DepartmentRecipientPolicy.objects.filter(department=department).first()
    if policy is None:
        return DepartmentRecipientPolicyState(restriction_enabled=False, approved_domains=())
    return DepartmentRecipientPolicyState(
        restriction_enabled=policy.restriction_enabled,
        approved_domains=tuple(
            policy.approved_domains.order_by("domain").values_list("domain", flat=True)
        ),
    )


@transaction.atomic
def set_department_recipient_policy(
    *,
    actor,
    department: Department,
    restriction_enabled: bool,
    approved_domains: list[str] | tuple[str, ...],
) -> DepartmentRecipientPolicy:
    _require_department_mail_manager(actor=actor, department=department)
    normalized_domains = tuple(
        sorted({normalize_recipient_domain(domain) for domain in approved_domains})
    )
    if len(normalized_domains) != len(approved_domains):
        raise ValidationError("Approved recipient domains must not contain duplicates.")
    if restriction_enabled and not normalized_domains:
        raise ValidationError(
            "At least one approved recipient domain is required when restriction is enabled."
        )
    policy, _ = DepartmentRecipientPolicy.objects.select_for_update().get_or_create(
        department=department
    )
    existing_domains = tuple(
        policy.approved_domains.order_by("domain").values_list("domain", flat=True)
    )
    if policy.restriction_enabled == restriction_enabled and existing_domains == normalized_domains:
        return policy
    policy.restriction_enabled = restriction_enabled
    policy.updated_by = actor
    policy.save(update_fields=("restriction_enabled", "updated_by", "updated_at"))
    policy.approved_domains.all().delete()
    DepartmentRecipientDomain.objects.bulk_create(
        [DepartmentRecipientDomain(policy=policy, domain=domain) for domain in normalized_domains]
    )
    record_event(
        action="outbound_mail.department_recipient_policy_changed",
        actor_user=actor,
        department=department,
        target_type="department_recipient_policy",
        target_uuid=policy.id,
        metadata={
            "restriction_enabled": restriction_enabled,
            "approved_domain_count": len(normalized_domains),
        },
    )
    return policy


def is_department_recipient_allowed(*, department: Department, recipient_email: str) -> bool:
    """Exact-domain policy query for later delivery enforcement; malformed inputs fail closed."""
    policy = DepartmentRecipientPolicy.objects.filter(
        department=department, restriction_enabled=True
    ).first()
    if policy is None:
        return True
    if (
        not isinstance(recipient_email, str)
        or recipient_email != recipient_email.strip()
        or recipient_email.count("@") != 1
    ):
        return False
    _, domain = recipient_email.rsplit("@", 1)
    try:
        normalized = normalize_recipient_domain(domain)
    except ValidationError:
        return False
    return policy.approved_domains.filter(domain=normalized).exists()
