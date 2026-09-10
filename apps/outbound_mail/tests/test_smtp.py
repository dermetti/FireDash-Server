import smtplib
import ssl

import pytest

from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.network_policy import ResolvedSmtpDestination
from apps.outbound_mail.runtime import (
    MailAddress,
    MailAttachment,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.outbound_mail.smtp import SmtpEffectiveConfiguration, SmtpProvider


class FakeConnection:
    def __init__(self, *, send_result: int = 1, open_error: Exception | None = None) -> None:
        self.send_result = send_result
        self.open_error = open_error
        self.messages = []
        self.opened = False
        self.closed = False

    def send_messages(self, messages) -> int:
        self.messages.extend(messages)
        return self.send_result

    def open(self) -> bool:
        self.opened = True
        if self.open_error:
            raise self.open_error
        return True

    def close(self) -> None:
        self.closed = True


def _configuration(*, tls_mode: str = "STARTTLS") -> SmtpEffectiveConfiguration:
    return SmtpEffectiveConfiguration(
        host="smtp.example.test",
        port=587,
        tls_mode=tls_mode,
        sender_name="Configured sender",
        sender_email="sender@example.test",
        username="smtp-user",
        password="smtp-password",
        timeout=12.0,
    )


def _message() -> OutboundMessage:
    return OutboundMessage(
        sender=MailAddress(display_name="Caller", email="caller@example.test"),
        recipient=MailAddress(display_name="Recipient", email="recipient@example.test"),
        subject="Report",
        body="Plain text report",
        attachments=(
            MailAttachment(filename="report.pdf", mime_type="application/pdf", content=b"\x00pdf"),
        ),
    )


def _public_destination(host: str, port: int) -> ResolvedSmtpDestination:
    return ResolvedSmtpDestination(hostname=host, addresses=("8.8.8.8",))


@pytest.mark.parametrize(
    "tls_mode,expected_tls,expected_ssl",
    [
        (SystemMailConfiguration.SmtpTlsMode.STARTTLS, True, False),
        (SystemMailConfiguration.SmtpTlsMode.IMPLICIT_TLS, False, True),
    ],
)
def test_smtp_maps_canonical_message_in_memory_for_both_approved_tls_modes(
    tls_mode, expected_tls, expected_ssl
) -> None:
    connection = FakeConnection()
    options = {}

    def factory(**kwargs):
        options.update(kwargs)
        return connection

    provider = SmtpProvider(
        configuration=_configuration(tls_mode=tls_mode),
        connection_factory=factory,
        destination_resolver=_public_destination,
    )
    result = provider.send(_message())

    assert result.provider == "SMTP"
    assert result.provider_message_id is None
    assert options["use_tls"] is expected_tls
    assert options["use_ssl"] is expected_ssl
    assert options["timeout"] == 12.0
    assert options["pinned_addresses"] == ("8.8.8.8",)
    assert "ssl_context" not in options
    email = connection.messages[0]
    assert email.to == ["recipient@example.test"]
    assert email.body == "Plain text report"
    assert email.attachments[0].filename == "report.pdf"
    assert email.attachments[0].content == b"\x00pdf"


@pytest.mark.parametrize(
    "error,error_type",
    [
        (TimeoutError(), ProviderUnavailableError),
        (OSError(), ProviderUnavailableError),
        (smtplib.SMTPAuthenticationError(535, b"credential response"), ProviderConfigurationError),
        (ssl.SSLError("TLS response"), ProviderConfigurationError),
        (smtplib.SMTPDataError(421, b"temporary response"), ProviderUnavailableError),
        (smtplib.SMTPDataError(550, b"rejected response"), MessageRejectedError),
        (
            smtplib.SMTPRecipientsRefused({"recipient@example.test": (550, b"rejected")}),
            MessageRejectedError,
        ),
    ],
)
def test_smtp_failures_are_sanitized_and_never_retried(error, error_type) -> None:
    connection = FakeConnection()

    def factory(**kwargs):
        return connection

    provider = SmtpProvider(
        configuration=_configuration(),
        connection_factory=factory,
        destination_resolver=_public_destination,
    )
    connection.send_messages = lambda messages: (_ for _ in ()).throw(error)
    with pytest.raises(error_type) as raised:
        provider.send(_message())
    assert "response" not in str(raised.value)
    assert "smtp-password" not in str(raised.value)


def test_smtp_verification_opens_secure_session_without_sending() -> None:
    connection = FakeConnection()
    provider = SmtpProvider(
        configuration=_configuration(),
        connection_factory=lambda **kwargs: connection,
        destination_resolver=_public_destination,
    )
    provider.verify()
    assert connection.opened and connection.closed
    assert connection.messages == []


def test_smtp_rejects_unapproved_transport_mode() -> None:
    provider = SmtpProvider(configuration=_configuration(tls_mode="PLAINTEXT"))
    with pytest.raises(ProviderConfigurationError):
        provider.verify()
