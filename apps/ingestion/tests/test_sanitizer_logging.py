"""Safe structured logging for rejected PDF sanitizer stages."""

import logging
import uuid
from types import SimpleNamespace

from apps.ingestion.services import _log_sanitizer_failure, _sanitize_log_filename
from apps.reference_data.pdf_sandbox import PdfSanitizerError


def _log_messages(caplog):
    return [m for m in caplog.messages if "PDF sanitizer rejected member" in m]


def test_sanitize_log_filename_is_safe_and_truncated():
    assert _sanitize_log_filename("plan.pdf") == "plan.pdf"
    assert _sanitize_log_filename("bad\nname.pdf") == "bad?name.pdf"
    assert _sanitize_log_filename("tab\tname.pdf") == "tab?name.pdf"
    assert len(_sanitize_log_filename("x" * 500)) == 200


def test_sanitizer_failure_logs_metadata_but_not_payload(caplog):
    batch_id = str(uuid.uuid4())
    error = PdfSanitizerError("PDF sanitizer rejected the document.")
    with caplog.at_level(logging.WARNING):
        _log_sanitizer_failure(
            batch=SimpleNamespace(id=batch_id),
            domain="fire_plans",
            filename="plan.pdf",
            source_sha256="a" * 64,
            job_uuid="job-1",
            stage="sanitize",
            error=error,
            input_bytes=123,
        )

    messages = _log_messages(caplog)
    assert len(messages) == 1
    message = messages[0]
    assert f"batch_id={batch_id}" in message
    assert "domain=fire_plans" in message
    assert "filename='plan.pdf'" in message
    assert f"source_sha256={'a' * 64}" in message
    assert "sanitizer_job=job-1" in message
    assert "stage=sanitize" in message
    assert "exception=PdfSanitizerError" in message
    assert "code=sanitizer_failure" in message
    assert "input_bytes=123" in message
    # The error detail string and any PDF payload must never be logged.
    assert "PDF sanitizer rejected the document." not in message


def test_sanitizer_failure_never_logs_sensitive_payload(caplog):
    secret_pdf = b"%PDF-SECRET-CONTENT-12345"
    with caplog.at_level(logging.WARNING):
        _log_sanitizer_failure(
            batch=SimpleNamespace(id=str(uuid.uuid4())),
            domain="fire_plans",
            filename="plan.pdf",
            source_sha256="b" * 64,
            job_uuid="",
            stage="quarantine_write",
            error=OSError("permission denied on /secret/path"),
            input_bytes=len(secret_pdf),
        )

    log_text = "\n".join(_log_messages(caplog))
    assert "SECRET-CONTENT" not in log_text
    assert "/secret/path" not in log_text
    assert "permission denied" not in log_text
