import base64
import json

import pytest
import requests
from django.test import override_settings

from apps.accounts.models import User
from apps.authorization.models import SystemRole
from apps.outbound_mail.brevo import BrevoProvider
from apps.outbound_mail.http_transport import (
    HttpResponse,
    OutboundMailHttpTransport,
    OutboundMailTransportError,
    validate_outbound_mail_https_proxy,
)
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.providers import resolve_runtime_provider
from apps.outbound_mail.runtime import (
    MailAddress,
    MailAttachment,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.outbound_mail.services import configure_brevo, replace_brevo_api_key


class FakeBrevoTransport:
    def __init__(
        self, *, status_code: int = 201, body: bytes = b'{"messageId":"opaque-id"}'
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.api_key_was_used = False
        self.payload = None
        self.get_url = None

    def post_json(self, *, url: str, headers: dict[str, str], payload: dict) -> HttpResponse:
        self.api_key_was_used = headers.get("api-key") == "test-brevo-key"
        self.payload = payload
        return HttpResponse(status_code=self.status_code, body=self.body)

    def get_json(self, *, url: str, headers: dict[str, str]) -> HttpResponse:
        self.get_url = url
        self.api_key_was_used = headers.get("api-key") == "test-brevo-key"
        return HttpResponse(status_code=self.status_code, body=self.body)


class FailingBrevoTransport:
    def post_json(self, *, url: str, headers: dict[str, str], payload: dict) -> HttpResponse:
        raise OutboundMailTransportError()


@pytest.fixture
def actor(db):
    user = User.objects.create_user(email="brevo-admin@example.test", display_name="System admin")
    SystemRole.objects.create(user=user)
    return user


@pytest.fixture
def configured_brevo(actor, tmp_path):
    keyring = tmp_path / "application-secret-kek-ring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"k" * 32).decode("ascii")}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        configure_brevo(
            actor=actor, sender_name="Configured sender", sender_email="sender@example.test"
        )
        replace_brevo_api_key(actor=actor, api_key="test-brevo-key")
        yield


def _message() -> OutboundMessage:
    return OutboundMessage(
        sender=MailAddress(display_name="Caller sender", email="caller@example.test"),
        recipient=MailAddress(display_name="Report recipient", email="recipient@example.test"),
        subject="Report",
        body="Plain text report",
        attachments=(
            MailAttachment(
                filename="report.pdf", mime_type="application/pdf", content=b"\x00\xffpdf"
            ),
        ),
    )


def test_brevo_maps_canonical_message_and_uses_configured_encrypted_key(configured_brevo) -> None:
    transport = FakeBrevoTransport()
    provider = BrevoProvider.from_system_configuration(transport=transport)
    result = provider.send(_message())

    assert result.provider == "BREVO"
    assert result.provider_message_id == "opaque-id"
    assert transport.api_key_was_used
    assert transport.payload == {
        "sender": {"name": "Configured sender", "email": "sender@example.test"},
        "to": [{"name": "Report recipient", "email": "recipient@example.test"}],
        "subject": "Report",
        "textContent": "Plain text report",
        "attachment": [{"name": "report.pdf", "content": "AP9wZGY="}],
    }
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert "test-brevo-key" not in configuration.brevo_api_key_encrypted
    assert "test-brevo-key" not in repr(provider)


def test_brevo_is_resolved_through_the_generic_provider_registry(configured_brevo) -> None:
    provider = resolve_runtime_provider(provider="BREVO")
    assert isinstance(provider, BrevoProvider)
    assert provider.provider_id == "BREVO"


def test_brevo_verification_uses_non_delivery_account_endpoint(configured_brevo) -> None:
    transport = FakeBrevoTransport(status_code=200, body=b'{"sensitive":"response"}')
    provider = BrevoProvider.from_system_configuration(transport=transport)
    provider.verify()
    assert transport.get_url == "https://api.brevo.com/v3/account"
    assert transport.api_key_was_used


@pytest.mark.parametrize(
    "status_code,error_type",
    [
        (429, ProviderUnavailableError),
        (500, ProviderUnavailableError),
        (401, ProviderConfigurationError),
        (402, ProviderConfigurationError),
        (403, ProviderConfigurationError),
        (400, MessageRejectedError),
        (422, MessageRejectedError),
        (418, ProviderUnavailableError),
    ],
)
def test_brevo_statuses_map_to_sanitized_provider_failures(
    configured_brevo, status_code, error_type
) -> None:
    provider = BrevoProvider.from_system_configuration(
        transport=FakeBrevoTransport(status_code=status_code, body=b"sensitive provider response")
    )
    with pytest.raises(error_type) as error:
        provider.send(_message())
    assert "sensitive provider response" not in str(error.value)
    assert "recipient@example.test" not in str(error.value)


def test_brevo_transport_failure_is_temporary_without_retry(configured_brevo) -> None:
    provider = BrevoProvider.from_system_configuration(transport=FailingBrevoTransport())
    with pytest.raises(ProviderUnavailableError):
        provider.send(_message())


class FakeResponse:
    def __init__(self, *, status_code: int = 201) -> None:
        self.status_code = status_code
        self.content = b'{"messageId":"opaque"}'


class FakeSession:
    def __init__(self, *, error: Exception | None = None, status_code: int = 201) -> None:
        self.calls = []
        self.error = error
        self.status_code = status_code
        self.trust_env = True

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return FakeResponse(status_code=self.status_code)


def test_http_transport_direct_and_proxy_routes_are_explicit() -> None:
    direct_session = FakeSession()
    direct = OutboundMailHttpTransport(proxy_url="", session=direct_session)
    direct.post_json(url="https://provider.example.test/send", headers={}, payload={})
    assert direct_session.trust_env is False
    assert direct_session.calls[0][1]["proxies"] is None
    assert direct_session.calls[0][1]["timeout"] == (5.0, 15.0)
    assert direct_session.calls[0][1]["allow_redirects"] is False

    proxy_session = FakeSession()
    proxy = OutboundMailHttpTransport(
        proxy_url="http://proxy.example.test:3128", session=proxy_session
    )
    proxy.post_json(url="https://provider.example.test/send", headers={}, payload={})
    assert proxy_session.calls[0][1]["proxies"] == {"https": "http://proxy.example.test:3128"}


@pytest.mark.parametrize("value", ["socks5://proxy.example.test:1080", "http:///missing-host"])
def test_proxy_validation_uses_the_transport_routing_contract(value: str) -> None:
    with pytest.raises(OutboundMailTransportError):
        validate_outbound_mail_https_proxy(value)


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_http_transport_proxy_failure_does_not_fall_back_to_direct(error) -> None:
    session = FakeSession(error=error)
    transport = OutboundMailHttpTransport(
        proxy_url="http://proxy.example.test:3128", session=session
    )
    with pytest.raises(OutboundMailTransportError):
        transport.post_json(url="https://provider.example.test/send", headers={}, payload={})
    assert len(session.calls) == 1
    assert session.calls[0][1]["proxies"] == {"https": "http://proxy.example.test:3128"}


def test_brevo_verification_uses_the_shared_proxy_aware_transport() -> None:
    session = FakeSession(status_code=200)
    provider = BrevoProvider(
        sender_name="Sender",
        sender_email="sender@example.test",
        api_key="test-brevo-key",
        transport=OutboundMailHttpTransport(
            proxy_url="http://proxy.example.test:3128", session=session
        ),
    )
    provider.verify()
    args, kwargs = session.calls[0]
    assert args == ("GET", "https://api.brevo.com/v3/account")
    assert kwargs["proxies"] == {"https": "http://proxy.example.test:3128"}
    assert kwargs["allow_redirects"] is False
