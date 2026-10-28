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
        ceilings = {
            "MAX_REQUEST_BYTES": 128 * 1024 * 1024,
            "MAX_JSON_BYTES": 1024 * 1024,
            "MAX_JPEG_BYTES": 8 * 1024 * 1024,
            "MAX_ATTACHMENTS": 32,
            "MAX_IMAGE_PIXELS": 32_000_000,
            "RETENTION_HOURS": 168,
        }
        if type(value) is int and value > ceilings[name]:
            errors.append(
                Error(f"REPORT_TRANSFER_{name} exceeds its ceiling.", id="report_transfer.E002")
            )
    return errors


class ReportTransferConfig(AppConfig):
    name = "apps.report_transfer"
