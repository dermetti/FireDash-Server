import socket
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.outbound_mail.network_policy import (
    OutboundNetworkPolicyError,
    _connect_pinned,
    resolve_smtp_destination,
)
from apps.outbound_mail.runtime import (
    MailAddress,
    OutboundMessage,
    ProviderConfigurationError,
)
from apps.outbound_mail.smtp import SmtpEffectiveConfiguration, SmtpProvider


def _records(*addresses: str):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (address, 587),
        )
        for address in addresses
    ]


def _resolver_for(addresses):
    return lambda *_args, **_kwargs: addresses


def test_smtp_policy_allows_public_dns_answers_and_rejects_literals_and_non_global_addresses():
    destination = resolve_smtp_destination(
        "smtp.example.test", 587, address_resolver=_resolver_for(_records("8.8.8.8"))
    )
    assert destination.addresses == ("8.8.8.8",)
    for hostname, addresses in (
        ("127.0.0.1", _records("8.8.8.8")),
        ("smtp.example.test", _records("127.0.0.1")),
        ("smtp.example.test", _records("10.0.0.1")),
        ("smtp.example.test", _records("169.254.1.1")),
        ("smtp.example.test", _records("192.0.2.1")),
        ("smtp.example.test", _records("::1")),
        ("smtp.example.test", _records("fe80::1")),
        ("smtp.example.test", _records("2001:db8::1")),
    ):
        with pytest.raises(OutboundNetworkPolicyError):
            resolve_smtp_destination(hostname, 587, address_resolver=_resolver_for(addresses))


def test_mixed_or_rebinding_answers_cannot_change_the_pinned_destination():
    with pytest.raises(OutboundNetworkPolicyError):
        resolve_smtp_destination(
            "smtp.example.test",
            587,
            address_resolver=lambda *_args, **_kwargs: _records("8.8.8.8", "10.0.0.1"),
        )
    calls = 0

    def rebinding_resolver(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _records("8.8.8.8" if calls == 1 else "10.0.0.1")

    destination = resolve_smtp_destination(
        "smtp.example.test", 587, address_resolver=rebinding_resolver
    )
    assert destination.addresses == ("8.8.8.8",)
    assert calls == 1


def test_pinned_connect_uses_checked_ip_address_without_a_second_dns_lookup():
    connected_socket = object()
    with patch(
        "apps.outbound_mail.network_policy.socket.create_connection", return_value=connected_socket
    ) as connect:
        assert _connect_pinned(("8.8.8.8",), 587, 5, None) is connected_socket
    assert connect.call_args.args[0] == ("8.8.8.8", 587)


def test_deployment_exceptions_are_narrow_and_off_by_default():
    with pytest.raises(OutboundNetworkPolicyError):
        resolve_smtp_destination(
            "relay.example.test",
            587,
            address_resolver=lambda *_args, **_kwargs: _records("10.0.0.8"),
        )
    with override_settings(OUTBOUND_MAIL_SMTP_ALLOWED_HOSTS=frozenset({"relay.example.test"})):
        assert resolve_smtp_destination(
            "relay.example.test",
            587,
            address_resolver=lambda *_args, **_kwargs: _records("10.0.0.8"),
        ).addresses == ("10.0.0.8",)
    with override_settings(OUTBOUND_MAIL_SMTP_ALLOWED_NETWORKS=("10.20.30.0/24",)):
        assert resolve_smtp_destination(
            "relay.example.test",
            587,
            address_resolver=lambda *_args, **_kwargs: _records("10.20.30.8"),
        ).addresses == ("10.20.30.8",)
        with pytest.raises(OutboundNetworkPolicyError):
            resolve_smtp_destination(
                "relay.example.test",
                587,
                address_resolver=lambda *_args, **_kwargs: _records("10.20.31.8"),
            )


def test_smtp_send_and_verification_share_policy_and_never_open_on_rejection():
    configuration = SmtpEffectiveConfiguration(
        host="smtp.example.test",
        port=587,
        tls_mode="STARTTLS",
        sender_name="Sender",
        sender_email="sender@example.test",
        username="",
        password="",
        timeout=5,
    )
    opened = []

    def connection_factory(**_kwargs):
        opened.append(True)
        raise AssertionError("must not connect")

    def rejected(*_args, **_kwargs):
        raise OutboundNetworkPolicyError()

    provider = SmtpProvider(
        configuration=configuration,
        connection_factory=connection_factory,
        destination_resolver=rejected,
    )
    with pytest.raises(ProviderConfigurationError):
        provider.verify()
    with pytest.raises(ProviderConfigurationError):
        provider.send(
            OutboundMessage(
                sender=MailAddress("S", "s@example.test"),
                recipient=MailAddress("R", "r@example.test"),
                subject="Subject",
                body="Body",
            )
        )
    assert not opened
