"""Standalone worker; installed with pdf_validation.py, never the application.

Executed by systemd as fire_pdf_sanitizer using Debian's isolated interpreter.
Only the two fixed job mounts are available. No command-line paths are accepted.
"""

import json
import os

# Only the fixed qpdf executable is invoked with job-local paths, without a shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

# Python -I excludes the script directory. This directory and both installed
# files are root-owned and not writable by either application or sanitizer.
if __package__:
    from .pdf_validation import PdfValidationError, validate_pdf
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pdf_validation import (  # type: ignore[import-not-found,no-redef]
        PdfValidationError,
        validate_pdf,
    )

INPUT = Path("/run/fire-pdf-sanitizer/input.pdf")
OUTPUT = Path("/run/fire-pdf-sanitizer/output/sanitized.pdf")
QPDF = "/usr/bin/qpdf"


def process(source: Path, output: Path) -> str:
    with (output.parent / "request.json").open("rb") as stream:
        raw = stream.read(257)
    if len(raw) > 256:
        raise ValueError("Invalid worker policy.")
    policy = json.loads(raw)
    if (
        not isinstance(policy, dict)
        or set(policy) != {"strict", "max_input_bytes", "max_output_bytes", "max_pages"}
        or type(policy["strict"]) is not bool
        or any(
            type(policy[key]) is not int or not 0 <= policy[key] <= 2**31 - 1
            for key in ("max_input_bytes", "max_output_bytes", "max_pages")
        )
    ):
        raise ValueError("Invalid worker policy.")
    try:
        validate_pdf(source, original_filename="input.pdf", **policy)
        # Exit 2 is ambiguous; never translate process failure into content rejection.
        subprocess.run(  # nosec B603
            [
                QPDF,
                "--deterministic-id",
                "--object-streams=disable",
                "--warning-exit-0",
                str(source),
                str(output),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
        size, pages, digest = validate_pdf(output, **policy)
    except PdfValidationError as error:
        return f"ERR content {error.code}"
    return f"OK {size} {pages} {digest}"


def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("PDF parsing must never run as root.")
    result = process(INPUT, OUTPUT)
    # A crash, timeout or operational exception never publishes a result.
    with (OUTPUT.parent / "result.txt").open("x", encoding="ascii") as stream:
        stream.write(result + "\n")


if __name__ == "__main__":
    main()
