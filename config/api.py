"""DRF exception handling using RFC 9457 problem details."""

import json
import logging

from rest_framework import exceptions
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def _validation_error_codes(exc: exceptions.ValidationError) -> dict[str, list[str]] | None:
    """Return ``{field: [code, ...]}`` for a serializer ValidationError, else None.

    Only field names and DRF ``ErrorDetail.code`` values are collected; error
    messages and any submitted values are deliberately excluded.
    """
    if not isinstance(exc, exceptions.ValidationError):
        return None
    try:
        details = exc.get_full_details()
    except Exception:  # pragma: no cover - defensive; never leak via logging
        return None
    if not isinstance(details, dict):
        return None
    codes: dict[str, list[str]] = {}
    for field, field_errors in details.items():
        if isinstance(field_errors, list):
            codes[str(field)] = [
                str(item.get("code", "error")) if isinstance(item, dict) else "error"
                for item in field_errors
            ]
        elif isinstance(field_errors, dict):
            codes[str(field)] = ["nested"]
        else:
            codes[str(field)] = ["error"]
    return codes


def _log_api_validation_failure(*, exc, request, status_code: int) -> None:
    """Log only safe diagnostic metadata for API validation/parser failures."""
    request_id = str(getattr(request, "request_id", "") or "")
    method = str(getattr(request, "method", "") or "")
    path = str(getattr(request, "path", "") or "")
    error_codes = _validation_error_codes(exc)
    if error_codes is None:
        logger.warning(
            "API validation failed request_id=%s method=%s path=%s status=%s exception=%s code=%s",
            request_id,
            method,
            path,
            status_code,
            type(exc).__name__,
            str(getattr(exc, "default_code", "") or ""),
        )
        return
    logger.warning(
        "API validation failed request_id=%s method=%s path=%s status=%s exception=%s errors=%s",
        request_id,
        method,
        path,
        status_code,
        type(exc).__name__,
        json.dumps(error_codes, sort_keys=True),
    )


def problem_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return response
    request = context.get("request")
    if isinstance(exc, exceptions.ValidationError | exceptions.ParseError):
        _log_api_validation_failure(exc=exc, request=request, status_code=response.status_code)
    detail = response.data.get("detail", "Request could not be processed.")
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail)
    elif isinstance(detail, dict):
        detail = "Request validation failed."
    code = getattr(exc, "default_code", "request-error")
    payload = {
        "type": f"https://fire-backend.internal/problems/{code}",
        "title": response.status_text,
        "status": response.status_code,
        "code": code,
        "detail": str(detail),
        "request_id": str(getattr(request, "request_id", "")),
    }
    if hasattr(exc, "minimum_app_version"):
        payload["minimum_app_version"] = exc.minimum_app_version
    response.data = payload
    response["Content-Type"] = "application/problem+json"
    response.setdefault("Cache-Control", "no-store, private")
    response.content_type = "application/problem+json"
    return response
