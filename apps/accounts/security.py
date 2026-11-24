"""Credential-safe diagnostics, including middleware and exception-report failures."""

from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter

from apps.report_transfer.security import credentials, redact, request_credentials


class SetupSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        marker = request_credentials.set(credentials(request))
        if request_credentials.get():
            request.sensitive_post_parameters = "__ALL__"
        try:
            return self.get_response(request)
        finally:
            request_credentials.reset(marker)


class CredentialExceptionFilter(SafeExceptionReporterFilter):
    def get_traceback_frame_variables(self, request, tb_frame):
        if request_credentials.get() or (request is not None and credentials(request)):
            return [(name, "[redacted]") for name in tb_frame.f_locals]
        return super().get_traceback_frame_variables(request, tb_frame)


class CredentialExceptionReporter(ExceptionReporter):
    def _safe_report(self, render):
        marker = request_credentials.set(
            credentials(self.request) if self.request else request_credentials.get()
        )
        try:
            return redact(render())
        finally:
            request_credentials.reset(marker)

    def get_traceback_text(self):
        return self._safe_report(super().get_traceback_text)

    def get_traceback_html(self):
        return self._safe_report(super().get_traceback_html)
