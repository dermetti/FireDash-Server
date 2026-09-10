"""Brevo transactional-email adapter for the canonical outbound-mail contract."""

from __future__ import annotations

import base64
import json

from django.core.exceptions import ValidationError

from apps.application_secrets.crypto import ApplicationSecretError, decrypt_application_secret
from apps.outbound_mail.http_transport import OutboundMailHttpTransport, OutboundMailTransportError
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.providers import BREVO, register_runtime_provider_factory
from apps.outbound_mail.runtime import (
    MailAddress,
    MailSendResult,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.outbound_mail.services import BREVO_API_KEY_CONTEXT

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
_BREVO_ACCOUNT_URL = "https://api.brevo.com/v3/account"


class BrevoProvider:
    """Adapter containing all Brevo endpoint, payload, and response knowledge."""

    provider_id = BREVO

    def __init__(
        self,
        *,
        sender_name: str,
        sender_email: str,
        api_key: str,
        transport: OutboundMailHttpTransport,
    ) -> None:
        self._sender_name = sender_name
        self._sender_email = sender_email
        self._api_key = api_key
        self._transport = transport

    @property
    def sender_identity(self) -> MailAddress:
        return MailAddress(display_name=self._sender_name, email=self._sender_email)

    @classmethod
    def from_system_configuration(
        cls, *, transport: OutboundMailHttpTransport | None = None
    ) -> BrevoProvider:
        try:
            configuration = SystemMailConfiguration.objects.get(singleton=True)
            configuration.validate_brevo_sender_configuration()
            if not configuration.brevo_api_key_encrypted:
                raise ValidationError("Brevo API key is missing.")
            api_key = decrypt_application_secret(
                configuration.brevo_api_key_encrypted, context=BREVO_API_KEY_CONTEXT
            ).decode("utf-8")
        except (
            SystemMailConfiguration.DoesNotExist,
            ValidationError,
            ApplicationSecretError,
            UnicodeDecodeError,
        ):
            raise ProviderConfigurationError() from None
        return cls(
            sender_name=configuration.brevo_sender_name,
            sender_email=configuration.brevo_sender_email,
            api_key=api_key,
            transport=transport or OutboundMailHttpTransport(),
        )

    def send(self, message: OutboundMessage) -> MailSendResult:
        payload = {
            "sender": {"name": self._sender_name, "email": self._sender_email},
            "to": [{"name": message.recipient.display_name, "email": message.recipient.email}],
            "subject": message.subject,
            "textContent": message.body,
        }
        if message.attachments:
            payload["attachment"] = [
                {
                    "name": attachment.filename,
                    "content": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in message.attachments
            ]
        try:
            response = self._transport.post_json(
                url=_BREVO_SEND_URL,
                headers={"api-key": self._api_key, "accept": "application/json"},
                payload=payload,
            )
        except OutboundMailTransportError:
            raise ProviderUnavailableError() from None
        if response.status_code == 201:
            try:
                message_id = json.loads(response.body).get("messageId")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProviderUnavailableError() from None
            if not isinstance(message_id, str) or not message_id:
                raise ProviderUnavailableError()
            return MailSendResult(provider=self.provider_id, provider_message_id=message_id)
        if response.status_code in {429} or response.status_code >= 500:
            raise ProviderUnavailableError()
        if response.status_code in {401, 402, 403}:
            raise ProviderConfigurationError()
        if response.status_code in {400, 422}:
            raise MessageRejectedError()
        raise ProviderUnavailableError()

    def verify(self) -> None:
        """Authenticate against Brevo's non-delivery account endpoint only."""
        try:
            response = self._transport.get_json(
                url=_BREVO_ACCOUNT_URL,
                headers={"api-key": self._api_key, "accept": "application/json"},
            )
        except OutboundMailTransportError:
            raise ProviderUnavailableError() from None
        if response.status_code == 200:
            return
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailableError()
        if response.status_code in {401, 402, 403}:
            raise ProviderConfigurationError()
        if response.status_code in {400, 422}:
            raise MessageRejectedError()
        raise ProviderUnavailableError()


register_runtime_provider_factory(provider=BREVO, factory=BrevoProvider.from_system_configuration)
