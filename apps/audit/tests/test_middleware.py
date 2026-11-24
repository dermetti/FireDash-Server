import socket
from unittest.mock import Mock

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from apps.audit.middleware import RequestContextMiddleware


@pytest.fixture(autouse=True)
def unix_socket_family_on_windows(monkeypatch):
    # The Windows Python build lacks AF_UNIX; these are mocked WSGI sockets.
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


def unix_connection(path="/run/fire-backend/gunicorn.sock"):
    connection = Mock(spec=socket.socket)
    connection.family = getattr(socket, "AF_UNIX", None)
    connection.getsockname.return_value = path
    return connection


@override_settings(TRUSTED_PROXY_UNIX_SOCKET="/run/fire-backend/gunicorn.sock")
@pytest.mark.parametrize(
    "address,expected",
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8::1", "2001:db8::1"),
        (None, None),
        ("", None),
        ("bad", None),
        ("203.0.113.7, 198.51.100.8", None),
        ("203.0.113.7:80", None),
        ("fe80::1%eth0", None),
        ("203.000.113.7", None),
    ],
)
def test_protected_unix_socket_validates_single_nginx_address(address, expected):
    request = RequestFactory().get("/", REMOTE_ADDR="", HTTP_X_FORWARDED_FOR=address)
    request.META["gunicorn.socket"] = unix_connection()
    RequestContextMiddleware(lambda request: HttpResponse())(request)
    assert request.client_ip == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize("boundary", ["missing", "wrong_path", "tcp", "disabled", "closed"])
@override_settings(TRUSTED_PROXY_UNIX_SOCKET="/run/fire-backend/gunicorn.sock")
def test_empty_remote_address_does_not_trust_spoofed_headers(boundary, settings):
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="",
        HTTP_X_FORWARDED_FOR="203.0.113.7",
        HTTP_X_REAL_IP="203.0.113.7",
        HTTP_GUNICORN_SOCKET="/run/fire-backend/gunicorn.sock",
    )
    connection = unix_connection()
    if boundary == "wrong_path":
        connection.getsockname.return_value = "/tmp/other.sock"
    elif boundary == "tcp":
        connection.family = socket.AF_INET
    elif boundary == "disabled":
        settings.TRUSTED_PROXY_UNIX_SOCKET = ""
    elif boundary == "closed":
        connection.getsockname.side_effect = OSError
    if boundary != "missing":
        request.META["gunicorn.socket"] = connection
    RequestContextMiddleware(lambda request: HttpResponse())(request)
    assert request.client_ip is None  # type: ignore[attr-defined]


@override_settings(TRUSTED_PROXY_IPS={"127.0.0.1"})
@pytest.mark.parametrize("address", [None, "bad", "203.0.113.7, 198.51.100.8", "fe80::1%eth0"])
def test_trusted_tcp_rejects_malformed_or_ambiguous_forwarding(address):
    assert RequestContextMiddleware._client_ip("127.0.0.1", address) == "127.0.0.1"


@pytest.mark.parametrize("remote", [None, "", "not-an-ip", "fe80::1%eth0"])
def test_invalid_remote_address_cannot_establish_proxy_trust(remote):
    assert RequestContextMiddleware._client_ip(remote, "203.0.113.7") is None


@pytest.mark.django_db
@override_settings(
    TRUSTED_PROXY_UNIX_SOCKET="/run/fire-backend/gunicorn.sock", AUTH_THROTTLE_MAX_FAILURES=2
)
def test_unix_authentication_throttles_ip_across_accounts_and_account_across_ips(client):
    from apps.accounts.models import AuthenticationThrottle

    def attempt(account, ip):
        return client.post(
            "/accounts/login/",
            {"email": account, "password": "incorrect-password"},
            REMOTE_ADDR="",
            HTTP_X_FORWARDED_FOR=ip,
            **{"gunicorn.socket": unix_connection()},
        )

    attempt("one@example.test", "203.0.113.7")
    attempt("two@example.test", "203.0.113.7")
    assert b"temporarily unavailable" in attempt("three@example.test", "203.0.113.7").content
    assert AuthenticationThrottle.objects.filter(subject="IP", failure_count=2).exists()
    attempt("one@example.test", "203.0.113.8")
    assert b"temporarily unavailable" in attempt("one@example.test", "203.0.113.9").content


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
