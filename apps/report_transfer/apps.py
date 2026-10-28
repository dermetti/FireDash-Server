from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


@register()
def report_limits(app_configs, **kwargs):
    errors = []
    for name in (
        "MAX_REQUEST_BYTES",
        "MAX_JSON_BYTES",
        "MAX_JPEG_BYTES",
        "MAX_ATTACHMENTS",
        "MAX_IMAGE_PIXELS",
        "RETENTION_HOURS",
    ):
        value = getattr(settings, f"REPORT_TRANSFER_{name}")
        minimum = 0 if name == "MAX_ATTACHMENTS" else 1
        if type(value) is not int or value < minimum:
            errors.append(Error(f"REPORT_TRANSFER_{name} is invalid.", id="report_transfer.E001"))
    return errors


class ReportTransferConfig(AppConfig):
    name = "apps.report_transfer"
