"""Generic SMTP adapter for typed/effective outbound-mail configurations."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

from apps.application_secrets.crypto import ApplicationSecretError, decrypt_application_secret
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.network_policy import (
    OutboundNetworkPolicyError,
    resolve_smtp_destination,
)
from apps.outbound_mail.providers import SMTP, register_runtime_provider_factory
from apps.outbound_mail.runtime import (
    MailAddress,
    MailSendResult,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.outbound_mail.services import SMTP_PASSWORD_CONTEXT


@dataclass(frozen=True, repr=False)
class SmtpEffectiveConfiguration:
    host: str
    port: int
    tls_mode: str
    sender_name: str
    sender_email: str
    username: str
    password: str
    timeout: float

    def __repr__(self) -> str:
        return (
            "SmtpEffectiveConfiguration("
            f"host={self.host!r}, port={self.port}, tls_mode={self.tls_mode!r}, "
            f"sender_name={self.sender_name!r}, sender_email={self.sender_email!r}, "
            f"username_configured={bool(self.username)}, password=<redacted>)"
        )


def _map_smtp_failure(error: Exception):
    if isinstance(
        error, smtplib.SMTPAuthenticationError | smtplib.SMTPNotSupportedError | ssl.SSLError
    ):
        return ProviderConfigurationError()
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return MessageRejectedError()
    if isinstance(error, smtplib.SMTPResponseException):
        if 400 <= error.smtp_code < 500:
            return ProviderUnavailableError()
        return MessageRejectedError()
    if isinstance(error, TimeoutError | OSError | smtplib.SMTPServerDisconnected):
        return ProviderUnavailableError()
    if isinstance(error, smtplib.SMTPException):
        return ProviderConfigurationError()
    return ProviderUnavailableError()


class SmtpProvider:
    provider_id = SMTP

    def __init__(
        self,
        *,
        configuration: SmtpEffectiveConfiguration,
        connection_factory=get_connection,
        destination_resolver=resolve_smtp_destination,
    ) -> None:
        self._configuration = configuration
        self._connection_factory = connection_factory
        self._destination_resolver = destination_resolver

    @property
    def sender_identity(self) -> MailAddress:
        return MailAddress(
            display_name=self._configuration.sender_name,
            email=self._configuration.sender_email,
        )

    @classmethod
    def from_system_configuration(cls) -> SmtpProvider:
        try:
            configuration = SystemMailConfiguration.objects.get(singleton=True)
            configuration.validate_smtp_configuration()
            if bool(configuration.smtp_username) != bool(configuration.smtp_password_encrypted):
                raise ValueError
            password = ""
            if configuration.smtp_username:
                password = decrypt_application_secret(
                    configuration.smtp_password_encrypted, context=SMTP_PASSWORD_CONTEXT
                ).decode("utf-8")
            effective = SmtpEffectiveConfiguration(
                host=configuration.smtp_host,
                port=configuration.smtp_port,
                tls_mode=configuration.smtp_tls_mode,
                sender_name=configuration.smtp_sender_name,
                sender_email=configuration.smtp_sender_email,
                username=configuration.smtp_username,
                password=password,
                timeout=settings.OUTBOUND_MAIL_SMTP_TIMEOUT_SECONDS,
            )
        except (
            SystemMailConfiguration.DoesNotExist,
            ValueError,
            ApplicationSecretError,
            UnicodeDecodeError,
        ):
            raise ProviderConfigurationError() from None
        return cls(configuration=effective)

    def _connection(self):
        if self._configuration.timeout <= 0:
            raise ProviderConfigurationError()
        use_tls = self._configuration.tls_mode == SystemMailConfiguration.SmtpTlsMode.STARTTLS
        use_ssl = self._configuration.tls_mode == SystemMailConfiguration.SmtpTlsMode.IMPLICIT_TLS
        if not (use_tls or use_ssl):
            raise ProviderConfigurationError()
        try:
            destination = self._destination_resolver(
                self._configuration.host, self._configuration.port
            )
        except OutboundNetworkPolicyError:
            raise ProviderConfigurationError() from None
        return self._connection_factory(
            backend="apps.outbound_mail.network_policy.PinnedSmtpEmailBackend",
            host=self._configuration.host,
            port=self._configuration.port,
            username=self._configuration.username,
            password=self._configuration.password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=self._configuration.timeout,
            fail_silently=False,
            pinned_addresses=destination.addresses,
        )

    def send(self, message: OutboundMessage) -> MailSendResult:
        try:
            connection = self._connection()
            email = EmailMultiAlternatives(
                subject=message.subject,
                body=message.body,
                from_email=(
                    f"{self._configuration.sender_name} <{self._configuration.sender_email}>"
                ),
                to=[message.recipient.email],
                connection=connection,
            )
            for attachment in message.attachments:
                email.attach(attachment.filename, attachment.content, attachment.mime_type)
            sent = connection.send_messages([email])
            if sent != 1:
                raise smtplib.SMTPServerDisconnected()
        except Exception as error:
            if isinstance(error, ProviderConfigurationError):
                raise
            raise _map_smtp_failure(error) from None
        return MailSendResult(provider=self.provider_id)

    def verify(self) -> None:
        """Open, negotiate TLS, and authenticate without constructing a message."""
        connection = None
        try:
            connection = self._connection()
            connection.open()
        except Exception as error:
            if isinstance(error, ProviderConfigurationError):
                raise
            raise _map_smtp_failure(error) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


register_runtime_provider_factory(provider=SMTP, factory=SmtpProvider.from_system_configuration)
