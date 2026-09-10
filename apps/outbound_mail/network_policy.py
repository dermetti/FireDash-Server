"""Pinned, deployment-governed SMTP destination selection."""

from __future__ import annotations

import ipaddress
import re
import smtplib
import socket
from dataclasses import dataclass
from functools import partial

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class OutboundNetworkPolicyError(Exception):
    """Sanitized destination-policy failure with no DNS or address detail."""

    def __init__(self) -> None:
        super().__init__("Outbound mail destination is not permitted.")


@dataclass(frozen=True)
class ResolvedSmtpDestination:
    hostname: str
    addresses: tuple[str, ...]


def _normalize_hostname(hostname: str) -> str:
    if not isinstance(hostname, str) or not hostname or hostname != hostname.strip():
        raise OutboundNetworkPolicyError()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise OutboundNetworkPolicyError()
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise OutboundNetworkPolicyError() from None
    if (
        not normalized
        or len(normalized) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in normalized.split("."))
    ):
        raise OutboundNetworkPolicyError()
    return normalized


def _deployment_exceptions() -> tuple[
    frozenset[str], tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
]:
    try:
        hosts = frozenset(
            _normalize_hostname(host) for host in settings.OUTBOUND_MAIL_SMTP_ALLOWED_HOSTS
        )
        networks = tuple(
            ipaddress.ip_network(value, strict=True)
            for value in settings.OUTBOUND_MAIL_SMTP_ALLOWED_NETWORKS
        )
    except (TypeError, ValueError):
        raise OutboundNetworkPolicyError() from None
    return hosts, networks


def resolve_smtp_destination(
    hostname: str,
    port: int,
    *,
    address_resolver=socket.getaddrinfo,
) -> ResolvedSmtpDestination:
    """Resolve and policy-check a hostname once for the subsequent pinned connect."""
    normalized_host = _normalize_hostname(hostname)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise OutboundNetworkPolicyError()
    allowed_hosts, allowed_networks = _deployment_exceptions()
    try:
        records = address_resolver(normalized_host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise OutboundNetworkPolicyError() from None
    addresses: list[str] = []
    for family, _, _, _, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, ValueError):
            raise OutboundNetworkPolicyError() from None
        allowed = (
            normalized_host in allowed_hosts
            or address.is_global
            or any(address in network for network in allowed_networks)
        )
        if not allowed:
            # A mixed answer is rejected rather than allowing a client library
            # to select a private answer after validation of a public one.
            raise OutboundNetworkPolicyError()
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise OutboundNetworkPolicyError()
    return ResolvedSmtpDestination(hostname=normalized_host, addresses=tuple(addresses))


def _connect_pinned(addresses: tuple[str, ...], port: int, timeout, source_address):
    last_error: OSError | None = None
    for address in addresses:
        try:
            # ``address`` is an IP literal derived above; socket therefore cannot
            # re-resolve the SMTP hostname between policy evaluation and connect.
            return socket.create_connection((address, port), timeout, source_address)
        except OSError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise OSError()


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port, *, pinned_addresses: tuple[str, ...], **kwargs) -> None:
        self._pinned_addresses = pinned_addresses
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        return _connect_pinned(self._pinned_addresses, port, timeout, self.source_address)


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(self, host, port, *, pinned_addresses: tuple[str, ...], **kwargs) -> None:
        self._pinned_addresses = pinned_addresses
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        sock = _connect_pinned(self._pinned_addresses, port, timeout, self.source_address)
        # Keep hostname SNI and certificate validation while connecting to the
        # already-policy-checked IP address.
        return self.context.wrap_socket(sock, server_hostname=self._host)


class PinnedSmtpEmailBackend(EmailBackend):
    """Django SMTP backend whose connection is pinned to checked DNS answers."""

    def __init__(self, *args, pinned_addresses: tuple[str, ...], **kwargs) -> None:
        self._pinned_addresses = pinned_addresses
        super().__init__(*args, **kwargs)

    @property
    def connection_class(self):
        base = _PinnedSMTPSSL if self.use_ssl else _PinnedSMTP
        return partial(base, pinned_addresses=self._pinned_addresses)
