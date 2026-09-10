"""Audited mutation and safe query services for system outbound-mail settings."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.application_secrets.crypto import encrypt_application_secret
from apps.audit.services import record_event
from apps.authorization.services import require_system_admin
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.providers import BREVO

BREVO_API_KEY_CONTEXT = "outbound-mail:api:brevo"
SMTP_PASSWORD_CONTEXT = "outbound-mail:smtp:password"


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


def _configuration() -> SystemMailConfiguration:
    try:
        configuration, _ = SystemMailConfiguration.objects.get_or_create(singleton=True)
    except IntegrityError:
        configuration = SystemMailConfiguration.objects.get(singleton=True)
    return configuration


def get_system_mail_configuration() -> SystemMailConfigurationState:
    configuration = _configuration()
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


@transaction.atomic
def configure_brevo(*, actor, sender_name: str, sender_email: str) -> SystemMailConfiguration:
    require_system_admin(actor)
    configuration = _locked_configuration()
    configuration.brevo_sender_name = sender_name.strip()
    configuration.brevo_sender_email = sender_email.strip()
    configuration.updated_by = actor
    configuration.full_clean(exclude=("brevo_api_key_encrypted", "smtp_password_encrypted"))
    configuration.validate_brevo_sender_configuration()
    configuration.save(
        update_fields=("brevo_sender_name", "brevo_sender_email", "updated_by", "updated_at")
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
    configuration.save(update_fields=("brevo_api_key_encrypted", "updated_by", "updated_at"))
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
    configuration.save(update_fields=("brevo_api_key_encrypted", "updated_by", "updated_at"))
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
    configuration.save(
        update_fields=(
            "smtp_host",
            "smtp_port",
            "smtp_tls_mode",
            "smtp_sender_name",
            "smtp_sender_email",
            "updated_by",
            "updated_at",
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
    configuration.save(
        update_fields=("smtp_username", "smtp_password_encrypted", "updated_by", "updated_at")
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
    configuration.save(
        update_fields=("smtp_username", "smtp_password_encrypted", "updated_by", "updated_at")
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
