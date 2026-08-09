import os

# The only child process is the fixed root-owned systemd sandbox wrapper.
import subprocess  # nosec B404
import sys
from pathlib import Path

from django.conf import settings


class PdfSanitizerError(RuntimeError):
    code = "sanitizer_failure"


class PdfSanitizerTimeout(PdfSanitizerError):
    code = "sanitizer_timeout"


def sanitize(*, quarantined_input: Path, sanitized_output: Path) -> None:
    """Call the systemd-sandboxed wrapper with no inherited application environment."""
    if sys.platform != "linux":
        raise PdfSanitizerError("PDF sanitizer requires the deployed Linux systemd sandbox.")
    wrapper = Path(settings.PDF_SANITIZER_WRAPPER)
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise PdfSanitizerError("PDF sanitizer wrapper is not installed or executable.")
    try:
        # The wrapper is fixed and all input/output paths are server-generated private paths.
        subprocess.run(  # nosec B603
            [
                str(wrapper),
                str(quarantined_input),
                str(sanitized_output),
                str(settings.PDF_SANITIZER_TIMEOUT_SECONDS),
                str(settings.PDF_SANITIZER_MEMORY_MAX_BYTES),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            timeout=settings.PDF_SANITIZER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise PdfSanitizerTimeout("PDF sanitizer timed out.") from error
    except subprocess.CalledProcessError as error:
        raise PdfSanitizerError("PDF sanitizer rejected the document.") from error
    if not sanitized_output.is_file():
        raise PdfSanitizerError("PDF sanitizer did not produce an output file.")
