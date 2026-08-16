"""DRF exception handling using RFC 9457 problem details."""

from rest_framework.views import exception_handler as drf_exception_handler


def problem_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return response
    request = context.get("request")
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
