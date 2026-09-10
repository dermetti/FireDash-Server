import base64
import json

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings

from apps.accounts.models import User
from apps.application_secrets.crypto import ApplicationSecretError, decrypt_application_secret
from apps.audit.models import AuditEvent
from apps.authorization.models import SystemRole
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.providers import BREVO, RUNTIME_PROVIDER_REGISTRY
from apps.outbound_mail.runtime import ProviderUnavailableError, VerificationOutcome
from apps.outbound_mail.services import (
    BREVO_API_KEY_CONTEXT,
    SMTP_PASSWORD_CONTEXT,
    clear_brevo_api_key,
    clear_smtp_credentials,
    configure_brevo,
    configure_smtp,
    get_system_mail_configuration,
    replace_brevo_api_key,
    replace_smtp_credentials,
    set_delivery_mode,
    verify_system_mail_configuration,
)


@pytest.fixture
def actor(db):
    user = User.objects.create_user(email="admin@example.test", display_name="System admin")
    SystemRole.objects.create(user=user)
    return user


@pytest.fixture
def application_secret_settings(tmp_path):
    keyring = tmp_path / "application-secret-kek-ring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"a" * 32).decode("ascii")}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield


def test_default_is_disabled_and_inactive_configurations_are_retained(
    actor, application_secret_settings
) -> None:
    initial = get_system_mail_configuration()
    assert initial.delivery_mode == SystemMailConfiguration.DeliveryMode.DISABLED
    assert SystemMailConfiguration.objects.filter(singleton=True).count() == 1

    configure_brevo(actor=actor, sender_name="FireDash", sender_email="sender@example.test")
    replace_brevo_api_key(actor=actor, api_key="brevo-secret")
    set_delivery_mode(
        actor=actor,
        delivery_mode=SystemMailConfiguration.DeliveryMode.API,
        api_provider=SystemMailConfiguration.ApiProvider.BREVO,
    )
    configure_smtp(
        actor=actor,
        host="smtp.example.test",
        port=587,
        tls_mode=SystemMailConfiguration.SmtpTlsMode.STARTTLS,
        sender_name="FireDash SMTP",
        sender_email="smtp-sender@example.test",
    )
    replace_smtp_credentials(actor=actor, username="smtp-user", password="smtp-secret")
    set_delivery_mode(actor=actor, delivery_mode=SystemMailConfiguration.DeliveryMode.SMTP)
    set_delivery_mode(actor=actor, delivery_mode=SystemMailConfiguration.DeliveryMode.DISABLED)

    state = get_system_mail_configuration()
    assert state.delivery_mode == SystemMailConfiguration.DeliveryMode.DISABLED
    assert state.brevo_api_key_configured and state.smtp_password_configured
    assert state.api_provider == SystemMailConfiguration.ApiProvider.BREVO


def test_activation_requires_complete_configuration(actor, application_secret_settings) -> None:
    with pytest.raises(ValidationError):
        set_delivery_mode(
            actor=actor,
            delivery_mode=SystemMailConfiguration.DeliveryMode.API,
            api_provider=SystemMailConfiguration.ApiProvider.BREVO,
        )
    with pytest.raises(ValidationError):
        set_delivery_mode(actor=actor, delivery_mode=SystemMailConfiguration.DeliveryMode.SMTP)

    with pytest.raises(PermissionDenied):
        configure_brevo(
            actor=User.objects.create_user(email="user@example.test", display_name="User"),
            sender_name="x",
            sender_email="x@example.test",
        )


def test_credentials_are_encrypted_with_distinct_contexts_and_are_redacted(
    actor, application_secret_settings
) -> None:
    configuration = replace_brevo_api_key(actor=actor, api_key="brevo-plaintext")
    configuration = replace_smtp_credentials(
        actor=actor, username="user", password="smtp-plaintext"
    )
    configuration.refresh_from_db()

    assert "brevo-plaintext" not in configuration.brevo_api_key_encrypted
    assert "smtp-plaintext" not in configuration.smtp_password_encrypted
    assert (
        decrypt_application_secret(
            configuration.brevo_api_key_encrypted, context=BREVO_API_KEY_CONTEXT
        )
        == b"brevo-plaintext"
    )
    assert (
        decrypt_application_secret(
            configuration.smtp_password_encrypted, context=SMTP_PASSWORD_CONTEXT
        )
        == b"smtp-plaintext"
    )
    with pytest.raises(ApplicationSecretError):
        decrypt_application_secret(
            configuration.brevo_api_key_encrypted, context=SMTP_PASSWORD_CONTEXT
        )
    assert "plaintext" not in str(configuration)
    assert "plaintext" not in repr(get_system_mail_configuration())


def test_credential_clear_and_audit_events_do_not_expose_secrets(
    actor, application_secret_settings
) -> None:
    replace_brevo_api_key(actor=actor, api_key="api-secret")
    clear_brevo_api_key(actor=actor)
    replace_smtp_credentials(actor=actor, username="user", password="smtp-secret")
    clear_smtp_credentials(actor=actor)
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert not configuration.brevo_api_key_configured
    assert not configuration.smtp_password_configured
    assert not configuration.smtp_username

    events = list(AuditEvent.objects.filter(target_uuid=configuration.id).order_by("action"))
    assert {event.action for event in events} >= {
        "outbound_mail.brevo_api_key_replaced",
        "outbound_mail.brevo_api_key_cleared",
        "outbound_mail.smtp_credentials_replaced",
        "outbound_mail.smtp_credentials_cleared",
    }
    rendered = repr([(event.action, event.metadata) for event in events])
    assert "api-secret" not in rendered
    assert "smtp-secret" not in rendered
    assert "app-secret:" not in rendered


@pytest.mark.parametrize(
    "host,port,tls_mode,sender_email",
    [
        ("", 587, SystemMailConfiguration.SmtpTlsMode.STARTTLS, "sender@example.test"),
        (
            "smtp.example.test",
            0,
            SystemMailConfiguration.SmtpTlsMode.STARTTLS,
            "sender@example.test",
        ),
        ("smtp.example.test", 587, "PLAINTEXT", "sender@example.test"),
        ("smtp.example.test", 587, SystemMailConfiguration.SmtpTlsMode.STARTTLS, "not-an-email"),
    ],
)
def test_smtp_transport_and_sender_validation_reject_invalid_values(
    actor, application_secret_settings, host, port, tls_mode, sender_email
) -> None:
    with pytest.raises(ValidationError):
        configure_smtp(
            actor=actor,
            host=host,
            port=port,
            tls_mode=tls_mode,
            sender_name="FireDash",
            sender_email=sender_email,
        )


def test_smtp_authentication_requires_a_paired_username_and_password(
    actor, application_secret_settings
) -> None:
    with pytest.raises(ValidationError):
        replace_smtp_credentials(actor=actor, username="user", password="")
    with pytest.raises(ValidationError):
        replace_smtp_credentials(actor=actor, username="", password="password")


def test_verification_is_observational_and_configuration_changes_invalidate_it(
    actor, application_secret_settings
) -> None:
    configure_brevo(actor=actor, sender_name="FireDash", sender_email="sender@example.test")
    replace_brevo_api_key(actor=actor, api_key="verification-secret")
    set_delivery_mode(
        actor=actor,
        delivery_mode=SystemMailConfiguration.DeliveryMode.API,
        api_provider=BREVO,
    )

    class FakeProvider:
        provider_id = BREVO

        def verify(self) -> None:
            return None

    RUNTIME_PROVIDER_REGISTRY[BREVO] = FakeProvider()
    try:
        result = verify_system_mail_configuration(actor=actor)
    finally:
        RUNTIME_PROVIDER_REGISTRY.pop(BREVO, None)
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert result.outcome == VerificationOutcome.SUCCESS
    assert result.diagnostic_code == "verified"
    assert configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.API
    assert configuration.api_provider == BREVO
    assert configuration.verification_provider == BREVO
    assert configuration.last_verified_at is not None

    configure_brevo(actor=actor, sender_name="Changed sender", sender_email="sender@example.test")
    configuration.refresh_from_db()
    assert not configuration.last_verification_outcome
    assert configuration.last_verified_at is None
    assert not configuration.last_verification_code


def test_failed_verification_persists_only_sanitized_metadata(
    actor, application_secret_settings
) -> None:
    configure_brevo(actor=actor, sender_name="FireDash", sender_email="sender@example.test")
    replace_brevo_api_key(actor=actor, api_key="verification-secret")
    set_delivery_mode(
        actor=actor,
        delivery_mode=SystemMailConfiguration.DeliveryMode.API,
        api_provider=BREVO,
    )

    class FailingProvider:
        provider_id = BREVO

        def verify(self) -> None:
            raise ProviderUnavailableError()

    RUNTIME_PROVIDER_REGISTRY[BREVO] = FailingProvider()
    try:
        result = verify_system_mail_configuration(actor=actor)
    finally:
        RUNTIME_PROVIDER_REGISTRY.pop(BREVO, None)
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert result.outcome == VerificationOutcome.FAILED
    assert result.diagnostic_code == "provider_unavailable"
    assert configuration.verification_provider == BREVO
    assert "verification-secret" not in repr(configuration)
    assert "verification-secret" not in repr(
        AuditEvent.objects.filter(target_uuid=configuration.id).values_list("metadata", flat=True)
    )
