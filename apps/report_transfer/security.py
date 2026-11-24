"""Portal response hardening and defense-in-depth application log redaction."""

import logging
import re
import traceback
from contextvars import ContextVar
from copy import copy
from urllib.parse import unquote

from django.conf import settings
from django.core.checks import Error, register
from django.http import HttpRequest, HttpResponse, QueryDict
from django.urls import ResolverMatch

TOKEN_PATH = re.compile(r"/reports/access/[^/\s?\"'<>]+")
SETUP_PATH = re.compile(r"/accounts/setup[^\s\"'<>]*", re.IGNORECASE)
request_credentials: ContextVar[tuple[str, ...]] = ContextVar("request_credentials", default=())


def _decoded_diagnostic(value: str) -> str:
    # Bound work for attacker-controlled nested encodings; never emit the
    # original undecoded value when that bound is reached.
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded
    return "[redacted encoded diagnostic]"


def credentials(request):
    values = [request.path, request.get_full_path()]
    values.extend(str(request.META.get(k, "")) for k in ("RAW_URI", "REQUEST_URI", "HTTP_REFERER"))
    found: list[str] = []
    for value in values:
        for match in SETUP_PATH.finditer(_decoded_diagnostic(value)):
            tail = match.group().removeprefix("/accounts/setup").strip("/")
            found.extend(part for part in re.split(r"[/?&=]", tail) if part)
    return tuple(found)


def redact(value):
    if isinstance(value, HttpRequest | BaseException):
        return redact(str(value))
    if isinstance(value, str):
        decoded = _decoded_diagnostic(value)
        if (
            SETUP_PATH.search(decoded)
            or TOKEN_PATH.search(decoded)
            or decoded == "[redacted encoded diagnostic]"
        ):
            value = decoded
        value = SETUP_PATH.sub("/accounts/setup/[redacted]", value)
        value = TOKEN_PATH.sub("/reports/access/[redacted]", value)
        for credential in request_credentials.get():
            value = value.replace(credential, "[redacted]")
        return value
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class TokenLogFilter(logging.Filter):
    def filter(self, record):
        request = getattr(record, "request", None)
        # django.server attaches a socket here, not an HttpRequest.
        if not isinstance(request, HttpRequest):
            request = None
        marker = request_credentials.set(credentials(request)) if request else None
        try:
            record.msg = str(record.msg)
            for key, value in vars(record).copy().items():
                if key not in ("request", "exc_info"):
                    setattr(record, key, redact(value))
            # Also cover custom objects passed through %s/%r arguments.
            rendered = record.getMessage()
            sanitized_message = redact(rendered)
            if rendered != sanitized_message:
                record.msg, record.args = sanitized_message, ()
            if record.exc_info:
                diagnostic = "".join(traceback.format_exception(*record.exc_info))
                sanitized = redact(diagnostic)
                if sanitized != diagnostic or request_credentials.get():
                    record.exc_text = sanitized
                    record.exc_info = None
            if request is not None and request_credentials.get():
                # Keep an HttpRequest for Django's diagnostic handlers, without
                # changing the live request still used for routing/validation.
                record.request = copy(request)
                record.request.path = redact(request.path)
                record.request.path_info = redact(request.path_info)
                record.request.META = redact(request.META)
                if hasattr(request, "environ"):
                    record.request.environ = record.request.META
                record.request.GET = QueryDict(mutable=True)
                for key, values in request.GET.lists():
                    record.request.GET.setlist(redact(key), redact(values))
                # Do not parse a rejected request body while reporting its error.
                record.request.POST = QueryDict()
                if request.resolver_match is not None:
                    match = request.resolver_match
                    # ResolverMatch deliberately disallows copying/pickling.
                    record.request.resolver_match = ResolverMatch(
                        match.func,
                        redact(match.args),
                        redact(match.kwargs),
                        url_name=match.url_name,
                        app_names=[*match.app_names],
                        namespaces=[*match.namespaces],
                        route=match.route,
                        captured_kwargs=redact(getattr(match, "captured_kwargs", None)),
                        extra_kwargs=redact(getattr(match, "extra_kwargs", None)),
                    )
        finally:
            if marker is not None:
                request_credentials.reset(marker)
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
            response["Referrer-Policy"] = "strict-origin"
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
