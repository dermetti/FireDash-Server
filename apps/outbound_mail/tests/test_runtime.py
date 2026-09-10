import pytest
from django.core.exceptions import ValidationError

from apps.outbound_mail.providers import (
    BREVO,
    RUNTIME_PROVIDER_FACTORY_REGISTRY,
    RUNTIME_PROVIDER_REGISTRY,
    register_runtime_provider,
    resolve_runtime_provider,
)
from apps.outbound_mail.runtime import (
    MailAddress,
    MailAttachment,
    MailProvider,
    MailSendResult,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)


def _message(*, attachments: tuple[MailAttachment, ...] = ()) -> OutboundMessage:
    return OutboundMessage(
        sender=MailAddress(display_name="FireDash", email="sender@example.test"),
        recipient=MailAddress(display_name="Recipient", email="recipient@example.test"),
        subject="Incident report",
        body="Plain-text report body",
        attachments=attachments,
    )


def test_canonical_message_supports_one_recipient_and_typed_attachments() -> None:
    attachment = MailAttachment(
        filename="report.pdf", mime_type="application/pdf", content=b"pdf bytes"
    )
    message = _message(attachments=(attachment,))
    assert message.sender.email == "sender@example.test"
    assert message.recipient.email == "recipient@example.test"
    assert message.attachments == (attachment,)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MailAddress(display_name="", email="recipient@example.test"),
        lambda: MailAddress(display_name="Recipient", email="not-an-email"),
        lambda: MailAttachment(filename="../report.pdf", mime_type="application/pdf", content=b"x"),
        lambda: MailAttachment(filename="report.pdf", mime_type="not a mime type", content=b"x"),
        lambda: MailAttachment(filename="report.pdf", mime_type="application/pdf", content="x"),
        lambda: OutboundMessage(
            sender=MailAddress(display_name="Sender", email="sender@example.test"),
            recipient=MailAddress(display_name="Recipient", email="recipient@example.test"),
            subject="",
            body="body",
        ),
        lambda: OutboundMessage(
            sender=MailAddress(display_name="Sender", email="sender@example.test"),
            recipient=MailAddress(display_name="Recipient", email="recipient@example.test"),
            subject="subject",
            body="",
        ),
        lambda: OutboundMessage(
            sender=MailAddress(display_name="Sender", email="sender@example.test"),
            recipient=MailAddress(display_name="Recipient", email="recipient@example.test"),
            subject="subject",
            body="body",
            attachments=[],
        ),
    ],
)
def test_canonical_values_reject_invalid_structure(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_attachment_bytes_and_message_body_are_redacted_from_normal_representations() -> None:
    attachment = MailAttachment(
        filename="report.pdf", mime_type="application/pdf", content=b"attachment-secret"
    )
    message = _message(attachments=(attachment,))
    assert "attachment-secret" not in repr(attachment)
    assert "attachment-secret" not in str(attachment)
    assert "attachment-secret" not in repr(message)
    assert "Plain-text report body" not in repr(message)


def test_normalized_success_and_failure_categories_are_stable() -> None:
    result = MailSendResult(provider=BREVO, provider_message_id="opaque-reference")
    assert result.provider == BREVO
    assert result.provider_message_id == "opaque-reference"
    assert ProviderUnavailableError().code == "provider_unavailable"
    assert MessageRejectedError().code == "message_rejected"
    assert ProviderConfigurationError().code == "provider_configuration"
    assert "attachment-secret" not in str(ProviderUnavailableError())


def test_registry_resolves_registered_provider_without_provider_branches() -> None:
    class FakeProvider:
        provider_id = BREVO

        def send(self, message: OutboundMessage) -> MailSendResult:
            return MailSendResult(provider=self.provider_id, provider_message_id="fake-1")

    provider = FakeProvider()
    assert isinstance(provider, MailProvider)
    assert BREVO not in RUNTIME_PROVIDER_REGISTRY
    register_runtime_provider(provider=BREVO, implementation=provider)
    try:
        resolved = resolve_runtime_provider(provider=BREVO)
        result = resolved.send(_message())
    finally:
        RUNTIME_PROVIDER_REGISTRY.pop(BREVO, None)
    assert result == MailSendResult(provider=BREVO, provider_message_id="fake-1")


def test_registry_rejects_unknown_and_unregistered_provider_deterministically() -> None:
    with pytest.raises(ProviderConfigurationError) as unknown:
        resolve_runtime_provider(provider="SENDGRID")
    assert unknown.value.code == "provider_configuration"
    assert unknown.value.detail == "Unsupported mail provider."

    factory = RUNTIME_PROVIDER_FACTORY_REGISTRY.pop(BREVO)
    try:
        with pytest.raises(ProviderConfigurationError) as unregistered:
            resolve_runtime_provider(provider=BREVO)
    finally:
        RUNTIME_PROVIDER_FACTORY_REGISTRY[BREVO] = factory
    assert unregistered.value.code == "provider_configuration"
    assert unregistered.value.detail == "Mail provider is not registered."
