from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from apps.audit.middleware import RequestContextMiddleware


@override_settings(TRUSTED_PROXY_IPS=frozenset({"127.0.0.1"}))
def test_forwarded_ip_is_ignored_from_an_untrusted_peer() -> None:
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="203.0.113.7", REMOTE_ADDR="198.51.100.8"
    )

    RequestContextMiddleware(lambda current_request: HttpResponse())(request)

    assert request.client_ip == "198.51.100.8"  # type: ignore[attr-defined]


@override_settings(TRUSTED_PROXY_IPS=frozenset({"127.0.0.1"}))
def test_forwarded_ip_is_accepted_only_from_local_nginx() -> None:
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.7", REMOTE_ADDR="127.0.0.1")

    RequestContextMiddleware(lambda current_request: HttpResponse())(request)

    assert request.client_ip == "203.0.113.7"  # type: ignore[attr-defined]
