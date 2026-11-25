import hashlib
import json
import re
import socket
import sys
from pathlib import Path

from django.conf import settings


class PdfSanitizerError(RuntimeError):
    """A sanitizer infrastructure/worker failure (fatal for the package)."""

    code = "sanitizer_failure"


class PdfSanitizerTimeout(PdfSanitizerError):
    """The sanitizer did not complete in time (infrastructure failure)."""

    code = "sanitizer_timeout"


class PdfSanitizerContentError(PdfSanitizerError):
    """A positive worker content rejection, safe to skip in package previews."""

    code = "sanitizer_content_rejected"

    def __init__(self, message: str, *, code: str = "sanitizer_content_rejected") -> None:
        super().__init__(message)
        self.code = code


_CONTENT_CODES = frozenset(
    {
        "invalid_pdf_signature",
        "invalid_pdf_mime",
        "input_too_large",
        "output_too_large",
        "encrypted_pdf",
        "malformed_pdf",
        "page_limit_exceeded",
        "prohibited_pdf_content",
    }
)
_SUCCESS = re.compile(r"OK ([1-9][0-9]{0,8}) (0|[1-9][0-9]{0,9}) ([0-9a-f]{64})")


_JOB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AF_UNIX: int = getattr(socket, "AF_UNIX", 1)


def _job_id_for(quarantined_input: Path) -> str:
    job_id = quarantined_input.parent.name
    if _JOB_UUID_RE.match(job_id) is None:
        raise PdfSanitizerError("PDF sanitizer job identifier is invalid.")
    return job_id


def _request_sanitization(socket_path: str, job_id: str, timeout: float) -> str:
    """Send the job UUID to the root broker and return its single status line."""
    with socket.socket(_AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(job_id.encode("ascii") + b"\n")
        client.shutdown(socket.SHUT_WR)
        reply = bytearray()
        while len(reply) <= 128:
            part = client.recv(129 - len(reply))
            if not part:
                break
            reply.extend(part)
        if len(reply) > 128 or not reply.endswith(b"\n"):
            raise PdfSanitizerError("PDF sanitizer returned an invalid result.")
    try:
        return reply.decode("ascii", "strict")[:-1]
    except UnicodeError as error:
        raise PdfSanitizerError("PDF sanitizer returned an invalid result.") from error


def sanitize(
    *, quarantined_input: Path, sanitized_output: Path, strict: bool = False
) -> tuple[int, int, str]:
    """Delegate to the root-owned socket-activated broker with no privilege elevation."""
    if sys.platform != "linux":
        raise PdfSanitizerError("PDF sanitizer requires the deployed Linux systemd sandbox.")
    job_id = _job_id_for(quarantined_input)
    socket_path = settings.PDF_SANITIZER_BROKER_SOCKET
    if not Path(socket_path).exists():
        raise PdfSanitizerError("PDF sanitizer broker socket is unavailable.")
    policy = {
        "strict": strict,
        "max_input_bytes": settings.MAX_PDF_INPUT_BYTES,
        "max_output_bytes": settings.MAX_PDF_OUTPUT_BYTES,
        "max_pages": settings.MAX_PDF_PAGES,
    }
    with (sanitized_output.parent / "request.json").open("x", encoding="ascii") as stream:
        json.dump(policy, stream)
    # The setgid output directory supplies the narrow sanitizer group.
    (sanitized_output.parent / "request.json").chmod(0o640)
    try:
        status = _request_sanitization(
            socket_path, job_id, settings.PDF_SANITIZER_TIMEOUT_SECONDS + 10
        )
    except TimeoutError as error:
        raise PdfSanitizerTimeout("PDF sanitizer timed out.") from error
    except OSError as error:
        raise PdfSanitizerError("PDF sanitizer broker is unavailable.") from error
    match = _SUCCESS.fullmatch(status)
    if match:
        if not sanitized_output.is_file():
            raise PdfSanitizerError("PDF sanitizer did not produce an output file.")
        size, pages, digest = int(match[1]), int(match[2]), match[3]
        if (
            size > settings.MAX_PDF_OUTPUT_BYTES
            or pages > settings.MAX_PDF_PAGES
            or (strict and pages == 0)
        ):
            raise PdfSanitizerError("PDF sanitizer metadata exceeds configured limits.")
        with sanitized_output.open("rb") as stream:
            signature = stream.read(5)
            stream.seek(0)
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            sanitized_output.stat().st_size != size
            or signature != b"%PDF-"
            or actual_digest != digest
        ):
            raise PdfSanitizerError("PDF sanitizer output failed integrity verification.")
        return size, pages, digest
    if status.startswith("ERR content ") and status[12:] in _CONTENT_CODES:
        raise PdfSanitizerContentError(
            "PDF safety validation rejected the document.", code=status[12:]
        )
    if status == "ERR timeout":
        raise PdfSanitizerTimeout("PDF sanitizer timed out.")
    raise PdfSanitizerError("PDF sanitizer broker rejected the request.")
