"""Portal response hardening and defense-in-depth application log redaction."""

import logging
import re

from django.conf import settings
from django.core.checks import Error, register
from django.http import HttpResponse

TOKEN_PATH = re.compile(r"/reports/access/[^/\s?\"'<>]+")


def redact(value):
    if isinstance(value, str):
        return TOKEN_PATH.sub("/reports/access/[redacted]", value)
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


class TokenLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.msg)
        record.args = redact(record.args)
        return True


class PortalSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        portal = request.path.startswith("/reports/")
        if portal:
            request.sensitive_post_parameters = "__ALL__"
        response = self.get_response(request)
        if portal:
            response["Cache-Control"] = "private, no-store"
            response["Referrer-Policy"] = "no-referrer"
            response["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; img-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            )
            response["X-Frame-Options"] = "DENY"
            response["X-Content-Type-Options"] = "nosniff"
            response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            for name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
                if name in response.cookies:
                    response.cookies[name]["secure"] = True
                    response.cookies[name]["samesite"] = "Lax"
                    if name == settings.SESSION_COOKIE_NAME:
                        response.cookies[name]["httponly"] = True
            # Django request/error loggers may render get_full_path after the
            # response; resolution is complete, so scrub their request object.
            if request.path.startswith("/reports/access/"):
                request.path = redact(request.path)
                request.path_info = redact(request.path_info)
                request.META["QUERY_STRING"] = ""
                for name in ("RAW_URI", "REQUEST_URI", "HTTP_REFERER"):
                    if name in request.META:
                        request.META[name] = "[redacted]"
        return response

    def process_exception(self, request, exception):
        if request.path.startswith("/reports/"):
            # Unexpected storage/database/template faults must not produce error
            # reports with bearer URLs, decrypted payload locals or credentials.
            logging.getLogger(__name__).error("Report portal request unavailable.")
            return HttpResponse("Berichtsportal vorübergehend nicht verfügbar.", status=503)
        return None


@register()
def portal_settings(app_configs, **kwargs):
    errors = []
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        errors.append(
            Error(
                "Report portal requires database-backed Django sessions.", id="report_transfer.E003"
            )
        )
    for name, ceiling in (("MAILS_PER_MINUTE", 10), ("MAILS_PER_HOUR", 60)):
        value = getattr(settings, "REPORT_TRANSFER_" + name)
        if type(value) is not int or not 1 <= value <= ceiling:
            errors.append(
                Error("Report transfer mail rate limit is invalid.", id="report_transfer.E004")
            )
    if not settings.SESSION_COOKIE_HTTPONLY or settings.SESSION_COOKIE_SAMESITE != "Lax":
        errors.append(
            Error(
                "Report portal requires secure session and CSRF cookies.", id="report_transfer.E005"
            )
        )
    return errors
