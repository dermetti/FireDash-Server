"""Debian container regression, executed as a disposable non-root identity.

Uses the installed production worker unchanged, without systemd or Django.
Host-level namespace, broker and credential acceptance remains mandatory.
"""

import hashlib
import io
import json
import os
import re
import runpy

# Only fixed native worker and qpdf commands are executed.
import subprocess  # nosec B404
from pathlib import Path

import magic
import pikepdf

HELPERS = Path("/usr/local/lib/fire-backend/pdf")
JOB = Path("/run/fire-pdf-sanitizer")


def main() -> None:
    assert os.geteuid() != 0, "native PDF parsing must not run as root"
    print(f"System pikepdf runtime: {pikepdf.__version__}", flush=True)
    benign, encrypted, active = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with pikepdf.new() as document:
        document.add_blank_page()
        document.save(benign)
        document.save(encrypted, encryption=pikepdf.Encryption(owner="test", user="test", R=6))
        # A name value without a prohibited dictionary key must be rejected.
        document.Root["/OpenAction"] = pikepdf.Dictionary(
            Next=pikepdf.Array([pikepdf.Dictionary(S=pikepdf.Name("/Launch"))])
        )
        document.save(active)
    content = benign.getvalue()
    start = content.index(b"startxref\n") + len(b"startxref\n")
    end = content.index(b"\n", start)
    recoverable = content[:start] + b"0" + content[end:]
    validate = runpy.run_path(str(HELPERS / "pdf_validation.py"))["validate_pdf"]
    limits = {"max_input_bytes": 1000000, "max_output_bytes": 1000000, "max_pages": 10}
    output = JOB / "output" / "sanitized.pdf"
    result_path = output.parent / "result.txt"
    for strict in (False, True):
        for label, data, rejection in (
            ("benign", content, None),
            ("non-PDF", b"not a PDF", "invalid_pdf_signature"),
            ("malformed", b"%PDF-1.7\ninvalid structure\n%%EOF\n", "malformed_pdf"),
            ("encrypted", encrypted.getvalue(), "encrypted_pdf"),
            ("prohibited-action", active.getvalue(), "prohibited_pdf_content"),
            ("recoverable-xref", recoverable, "malformed_pdf" if strict else None),
        ):
            output.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            source = JOB / "input.pdf"
            source.write_bytes(data)
            assert magic.from_file(str(source), mime=True) == (
                "text/plain" if label == "non-PDF" else "application/pdf"
            )
            (output.parent / "request.json").write_text(
                json.dumps({"strict": strict, **limits}), encoding="ascii"
            )
            # Fixed root-owned worker; no supplied commands.
            subprocess.run(  # nosec B603
                ["/usr/bin/python3", "-I", str(HELPERS / "pdf_worker.py")],
                check=True,
                timeout=60,
            )
            result = result_path.read_bytes()
            assert len(result) <= 128 and result.endswith(b"\n")
            if rejection:
                assert result == f"ERR content {rejection}\n".encode("ascii"), (label, result)
                assert not output.exists(), label
            else:
                assert re.fullmatch(rb"OK [1-9][0-9]* 1 [0-9a-f]{64}\n", result), result
                # Reopen normalized output through the shared strict policy,
                # and compare every reported field to actual output contents.
                size, pages, digest = validate(output, strict=True, **limits)
                assert result == f"OK {size} {pages} {digest}\n".encode("ascii")
                assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
                assert output.read_bytes() != data, "qpdf must actually normalize the input"
                # Fixed qpdf executable and synthetic output.
                subprocess.run(  # nosec B603
                    ["/usr/bin/qpdf", "--check", str(output)], check=True, timeout=15
                )
            print(f"PASS: {'strict' if strict else 'recovery'}: {label}", flush=True)


if __name__ == "__main__":
    main()
