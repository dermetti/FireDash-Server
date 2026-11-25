"""Explicit deployment smoke check; never creates source or publication records."""

import io
import sys

import pikepdf
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from apps.reference_data import storage
from apps.reference_data.pdf_sandbox import PdfSanitizerContentError
from apps.reference_data.pdf_validation import PdfValidationError


class Command(BaseCommand):
    help = "Verify real Linux MIME/PDF inspection and the isolated sanitizer broker."

    def handle(self, *args, **options):
        if sys.platform != "linux":
            raise CommandError("Native sanitizer verification requires the deployed Linux host.")
        benign, protected, active = io.BytesIO(), io.BytesIO(), io.BytesIO()
        with pikepdf.new() as pdf:
            pdf.add_blank_page()
            pdf.save(benign)
            pdf.save(protected, encryption=pikepdf.Encryption(owner="smoke", user="smoke", R=6))
            pdf.Root["/OpenAction"] = pikepdf.Dictionary(S=pikepdf.Name("/Launch"))
            pdf.save(active)
        content = benign.getvalue()
        start = content.index(b"startxref\n") + len(b"startxref\n")
        end = content.index(b"\n", start)
        recoverable = content[:start] + b"0" + content[end:]
        for strict in (False, True):
            mode = "strict" if strict else "recovery"
            for label, data, accepted in (
                ("benign", benign.getvalue(), True),
                ("non-PDF", b"not a PDF", False),
                ("malformed", b"%PDF-1.7\ninvalid structure\n%%EOF\n", False),
                ("password-protected", protected.getvalue(), False),
                ("prohibited-action-name", active.getvalue(), False),
                ("recoverable-xref", recoverable, not strict),
            ):
                source = output = None
                try:
                    source = storage.write_quarantine(
                        SimpleUploadedFile("smoke.pdf", data),
                        limit=settings.FILE_LIBRARY_MAX_FILE_BYTES,
                    )
                    try:
                        output = storage.output_path(job_id=source.parent.name)
                        storage.process_pdf(
                            source, output, original_filename="smoke.pdf", strict=strict
                        )
                    except (PdfValidationError, PdfSanitizerContentError):
                        if accepted:
                            raise CommandError(
                                "Native inspection rejected the benign PDF."
                            ) from None
                    else:
                        if not accepted:
                            raise CommandError(f"Native inspection accepted {label} input.")
                    self.stdout.write(f"PASS: {mode}: {label}")
                finally:
                    storage.cleanup(source)
                    storage.cleanup(output)
        self.stdout.write(
            self.style.SUCCESS(
                "Native inspection and broker checks passed; no canonical records were created."
            )
        )
