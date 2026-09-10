"""Shared no-retry HTTP transport for outbound-mail provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from django.conf import settings


class OutboundMailTransportError(Exception):
    """Sanitized transport failure; never carries an underlying response or request."""

    def __init__(self) -> None:
        super().__init__("Outbound mail transport failed.")


@dataclass(frozen=True, repr=False)
class HttpResponse:
    status_code: int
    body: bytes

    def __repr__(self) -> str:
        return (
            f"HttpResponse(status_code={self.status_code}, body=<redacted {len(self.body)} bytes>)"
        )


def _proxy_configuration(proxy_url: str) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OutboundMailTransportError()
    return {"https": proxy_url}


class OutboundMailHttpTransport:
    """One-shot JSON transport with explicit direct-or-proxy routing semantics."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        configured_proxy = settings.OUTBOUND_MAIL_HTTPS_PROXY if proxy_url is None else proxy_url
        self._proxies = _proxy_configuration(configured_proxy)
        self._timeout = (
            settings.OUTBOUND_MAIL_HTTP_CONNECT_TIMEOUT_SECONDS
            if connect_timeout is None
            else connect_timeout,
            settings.OUTBOUND_MAIL_HTTP_READ_TIMEOUT_SECONDS
            if read_timeout is None
            else read_timeout,
        )
        if self._timeout[0] <= 0 or self._timeout[1] <= 0:
            raise OutboundMailTransportError()
        self._session = session or requests.Session()
        # Never inherit ambient proxy configuration: direct/proxy behavior is a
        # deliberate deployment choice and must not have a fallback route.
        self._session.trust_env = False

    def post_json(self, *, url: str, headers: dict[str, str], payload: dict) -> HttpResponse:
        return self.request_json(method="POST", url=url, headers=headers, payload=payload)

    def get_json(self, *, url: str, headers: dict[str, str]) -> HttpResponse:
        return self.request_json(method="GET", url=url, headers=headers)

    def request_json(
        self, *, method: str, url: str, headers: dict[str, str], payload: dict | None = None
    ) -> HttpResponse:
        try:
            response = self._session.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
                proxies=self._proxies,
                # Authenticated provider requests never follow a redirect: a
                # redirect could otherwise move credentials to another origin.
                allow_redirects=False,
            )
        except requests.RequestException:
            raise OutboundMailTransportError() from None
        return HttpResponse(status_code=response.status_code, body=response.content)
