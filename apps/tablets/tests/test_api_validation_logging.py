"""Server-side diagnostic logging for API validation/parser failures.

These tests prove the RFC 9457 exception handler logs only safe metadata (field
names and DRF error codes) without ever leaking submitted values, tokens, or
request bodies.
"""

import logging
import uuid
from types import SimpleNamespace

from rest_framework import exceptions
from rest_framework.exceptions import ErrorDetail

from config.api import problem_exception_handler


def _request(method="POST", path="/api/v1/adoption/preview", request_id=None, data=None):
    return SimpleNamespace(
        method=method,
        path=path,
        request_id=request_id or str(uuid.uuid4()),
        data=data,
    )


def _context(request):
    return {"request": request, "view": None}


def _log_messages(caplog):
    return [m for m in caplog.messages if "API validation failed" in m]


def test_invalid_app_version_logs_field_and_code(caplog):
    request_id = str(uuid.uuid4())
    exc = exceptions.ValidationError(
        {"app_version": [ErrorDetail("Invalid version format.", code="invalid")]}
    )
    with caplog.at_level(logging.WARNING):
        response = problem_exception_handler(exc, _context(_request(request_id=request_id)))

    assert response.status_code == 400
    assert response.data["type"].startswith("https://fire-backend.internal/problems/")
    assert response.data["request_id"] == request_id
    messages = _log_messages(caplog)
    assert len(messages) == 1
    message = messages[0]
    assert f"request_id={request_id}" in message
    assert "method=POST" in message
    assert "path=/api/v1/adoption/preview" in message
    assert "status=400" in message
    assert "app_version" in message
    assert '"invalid"' in message
    assert "Invalid version format." not in message


def test_invalid_app_build_logs_field_and_code(caplog):
    exc = exceptions.ValidationError(
        {
            "app_build": [
                ErrorDetail("Ensure this value is greater than or equal to 1.", code="min_value")
            ]
        }
    )
    with caplog.at_level(logging.WARNING):
        problem_exception_handler(exc, _context(_request()))
    message = _log_messages(caplog)[0]
    assert "app_build" in message
    assert "min_value" in message


def test_invalid_hpke_public_key_logs_field_and_code(caplog):
    exc = exceptions.ValidationError(
        {"hpke_public_key": [ErrorDetail("Must be valid base64.", code="invalid")]}
    )
    with caplog.at_level(logging.WARNING):
        problem_exception_handler(exc, _context(_request()))
    message = _log_messages(caplog)[0]
    assert "hpke_public_key" in message
    assert '"invalid"' in message


def test_malformed_json_logs_safe_parser_diagnostic(caplog):
    exc = exceptions.ParseError(
        detail="JSON parse error - Expecting value: line 1 column 1 (char 0)"
    )
    with caplog.at_level(logging.WARNING):
        response = problem_exception_handler(exc, _context(_request()))
    assert response.status_code == 400
    message = _log_messages(caplog)[0]
    assert "ParseError" in message
    assert "parse_error" in message
    assert "Expecting value" not in message


def test_submitted_values_never_logged(caplog):
    secret_token = "SECRET-ADOPTION-TOKEN-123"
    secret_key = "AAAA-BASE64-HPKE-KEY-SECRET"
    exc = exceptions.ValidationError(
        {
            "token": [ErrorDetail("Invalid token.", code="invalid")],
            "hpke_public_key": [ErrorDetail("Must be valid base64.", code="invalid")],
        }
    )
    request = _request(
        data={"token": secret_token, "hpke_public_key": secret_key, "app_version": "1.0.0"}
    )
    with caplog.at_level(logging.WARNING):
        problem_exception_handler(exc, _context(request))
    log_text = "\n".join(_log_messages(caplog))
    assert secret_token not in log_text
    assert secret_key not in log_text
    assert "1.0.0" not in log_text
    assert "token" in log_text
    assert "hpke_public_key" in log_text


def test_non_validation_errors_are_not_logged(caplog):
    exc = exceptions.PermissionDenied("Installation authentication is required.")
    with caplog.at_level(logging.WARNING):
        response = problem_exception_handler(exc, _context(_request()))
    assert response.status_code == 403
    assert _log_messages(caplog) == []
