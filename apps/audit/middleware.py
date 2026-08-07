import uuid
from ipaddress import ip_address

from django.conf import settings


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid.uuid4()
        remote_address = request.META.get("REMOTE_ADDR", "")
        request.client_ip = self._client_ip(
            remote_address, request.META.get("HTTP_X_FORWARDED_FOR")
        )
        response = self.get_response(request)
        response["X-Request-ID"] = str(request.request_id)
        return response

    @staticmethod
    def _client_ip(remote_address: str, forwarded_for: str | None) -> str | None:
        try:
            remote_ip = ip_address(remote_address)
        except ValueError:
            return None
        if str(remote_ip) not in settings.TRUSTED_PROXY_IPS or not forwarded_for:
            return str(remote_ip)
        candidate = forwarded_for.split(",", 1)[0].strip()
        try:
            return str(ip_address(candidate))
        except ValueError:
            return str(remote_ip)
