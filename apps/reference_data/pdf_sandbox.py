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
    """The sanitizer positively typed this document as content (skippable).

    This is only raised when the broker has a reliable, positive content-rejection
    signal. The current qpdf/systemd interface cannot provide one, so this type is
    reserved for a future broker interface; ambiguous qpdf failures are always
    reported as ``PdfSanitizerError`` and remain fatal.
    """

    code = "sanitizer_content_rejected"


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
        reply = client.recv(64)
    return reply.decode("ascii", "strict").strip()


def sanitize(*, quarantined_input: Path, sanitized_output: Path) -> None:
    """Delegate to the root-owned socket-activated broker with no privilege elevation."""
    if sys.platform != "linux":
        raise PdfSanitizerError("PDF sanitizer requires the deployed Linux systemd sandbox.")
    job_id = _job_id_for(quarantined_input)
    socket_path = settings.PDF_SANITIZER_BROKER_SOCKET
    if not Path(socket_path).exists():
        raise PdfSanitizerError("PDF sanitizer broker socket is unavailable.")
    try:
        status = _request_sanitization(
            socket_path, job_id, settings.PDF_SANITIZER_TIMEOUT_SECONDS + 10
        )
    except TimeoutError as error:
        raise PdfSanitizerTimeout("PDF sanitizer timed out.") from error
    except OSError as error:
        raise PdfSanitizerError("PDF sanitizer broker is unavailable.") from error
    if status == "OK":
        if not sanitized_output.is_file():
            raise PdfSanitizerError("PDF sanitizer did not produce an output file.")
        return
    if status == "ERR failed":
        # Reserved for a future positively-typed content rejection. The broker
        # currently never emits this token because qpdf's exit status 2 does not
        # distinguish document content from operational failure; ambiguous qpdf
        # failures are reported as ``ERR output`` and remain fatal.
        raise PdfSanitizerContentError("PDF sanitizer rejected the document.")
    if status == "ERR timeout":
        raise PdfSanitizerTimeout("PDF sanitizer timed out.")
    raise PdfSanitizerError("PDF sanitizer broker rejected the request.")
