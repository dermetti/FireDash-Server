import socket
import uuid
from ipaddress import ip_address

from django.conf import settings


def _validated_ip_address(value: str) -> str:
    # Scoped IPv6 literals cannot be persisted as PostgreSQL inet addresses.
    if not isinstance(value, str) or "%" in value:
        raise ValueError("A plain IP address is required.")
    return str(ip_address(value))


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid.uuid4()
        remote_address = request.META.get("REMOTE_ADDR", "")
        # gunicorn.socket is server-created WSGI state, not an HTTP header.
        # Missing REMOTE_ADDR alone never establishes a trusted proxy boundary.
        connection = request.META.get("gunicorn.socket")
        unix_family = getattr(socket, "AF_UNIX", None)
        trusted_unix = False
        if (
            connection is not None
            and unix_family is not None
            and settings.TRUSTED_PROXY_UNIX_SOCKET
        ):
            try:
                trusted_unix = (
                    connection.family == unix_family
                    and connection.getsockname() == settings.TRUSTED_PROXY_UNIX_SOCKET
                )
            except (AttributeError, OSError):
                pass
        request.client_ip = self._client_ip(
            remote_address, request.META.get("HTTP_X_FORWARDED_FOR"), trusted_unix=trusted_unix
        )
        response = self.get_response(request)
        response["X-Request-ID"] = str(request.request_id)
        return response

    @staticmethod
    def _client_ip(
        remote_address: str, forwarded_for: str | None, *, trusted_unix: bool = False
    ) -> str | None:
        if trusted_unix:
            try:
                return _validated_ip_address((forwarded_for or "").strip())
            except ValueError:
                return None
        try:
            remote_ip = _validated_ip_address(remote_address)
        except ValueError:
            return None
        if remote_ip not in settings.TRUSTED_PROXY_IPS or not forwarded_for:
            return remote_ip
        candidate = forwarded_for.strip()
        try:
            return _validated_ip_address(candidate)
        except ValueError:
            return remote_ip
