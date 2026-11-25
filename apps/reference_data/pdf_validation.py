import hashlib
from pathlib import Path


class PdfValidationError(ValueError):
    """A PDF was rejected for a content/safety reason.

    ``code`` is a stable machine-readable reason code; ``message`` is a concise
    human-readable explanation.
    """

    def __init__(self, message: str, *, code: str = "invalid_pdf") -> None:
        super().__init__(message)
        self.code = code


PROHIBITED_NAMES = {
    "/JS",
    "/JavaScript",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/EmbeddedFiles",
    "/AF",
    "/Collection",
    "/RichMedia",
    "/Movie",
    "/Sound",
    "/XFA",
    "/GoToR",
    "/URI",
}


def validate_pdf(
    path: Path,
    *,
    original_filename: str | None = None,
    strict: bool = False,
    max_input_bytes: int | None = None,
    max_output_bytes: int | None = None,
    max_pages: int | None = None,
) -> tuple[int, int, str]:
    # Explicit limits let the root-owned worker reuse this policy without Django
    # or any application settings/environment. Native parsing is worker-only.
    if max_input_bytes is None or max_output_bytes is None or max_pages is None:
        from django.conf import settings

        max_input_bytes = settings.MAX_PDF_INPUT_BYTES
        max_output_bytes = settings.MAX_PDF_OUTPUT_BYTES
        max_pages = settings.MAX_PDF_PAGES
    if original_filename is not None and not original_filename.casefold().endswith(".pdf"):
        raise PdfValidationError("Only PDF uploads are accepted.", code="unsupported_extension")
    if path.stat().st_size > max_input_bytes and original_filename is not None:
        raise PdfValidationError(
            "PDF input exceeds the configured size limit.", code="input_too_large"
        )
    if path.stat().st_size > max_output_bytes and original_filename is None:
        raise PdfValidationError(
            "Sanitized PDF exceeds the configured size limit.", code="output_too_large"
        )
    with path.open("rb") as stream:
        signature = stream.read(5)
    if signature != b"%PDF-":
        raise PdfValidationError(
            "Upload does not have a PDF signature.", code="invalid_pdf_signature"
        )
    _validate_mime(path)
    return _inspect_pdf(path, strict=strict, max_pages=max_pages)


def _validate_mime(path: Path) -> None:
    try:
        import magic
    except ImportError as error:
        raise RuntimeError("PDF MIME dependency is unavailable.") from error
    try:
        mime_type = magic.from_file(str(path), mime=True)
    except Exception as error:
        raise RuntimeError("PDF MIME inspection failed.") from error
    if mime_type != "application/pdf":
        raise PdfValidationError(
            "Upload MIME type is not application/pdf.", code="invalid_pdf_mime"
        )


def _inspect_pdf(path: Path, *, strict: bool = False, max_pages: int) -> tuple[int, int, str]:
    import pikepdf

    try:
        with pikepdf.open(path, attempt_recovery=not strict) as document:
            if document.is_encrypted:
                raise PdfValidationError("Encrypted PDFs are not accepted.", code="encrypted_pdf")
            page_count = len(document.pages)
            if strict and (page_count == 0 or document.check_pdf_syntax()):
                raise PdfValidationError(
                    "PDF is empty or structurally invalid.", code="malformed_pdf"
                )
            if page_count > max_pages:
                raise PdfValidationError("PDF page limit exceeded.", code="page_limit_exceeded")
            if _contains_prohibited_content(document.Root):
                raise PdfValidationError(
                    "PDF contains prohibited active or external content.",
                    code="prohibited_pdf_content",
                )
    except pikepdf.PasswordError:
        raise PdfValidationError("Encrypted PDFs are not accepted.", code="encrypted_pdf") from None
    except PdfValidationError:
        raise
    except pikepdf.PdfError as error:
        raise PdfValidationError(
            "PDF is malformed or cannot be parsed.", code="malformed_pdf"
        ) from error
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path.stat().st_size, page_count, digest


def _contains_prohibited_content(value: object, seen: set[tuple[int, int]] | None = None) -> bool:
    if seen is None:
        seen = set()
    objgen = getattr(value, "objgen", None)
    if objgen and objgen != (0, 0):
        if objgen in seen:
            return False
        seen.add(objgen)
    import pikepdf

    if isinstance(value, pikepdf.Name):
        return str(value) in PROHIBITED_NAMES
    if isinstance(value, pikepdf.Dictionary | pikepdf.Stream):
        return any(
            str(key) in PROHIBITED_NAMES or _contains_prohibited_content(item, seen)
            for key, item in value.items()
        )
    if isinstance(value, pikepdf.Array):
        return any(_contains_prohibited_content(value[index], seen) for index in range(len(value)))
    # PDF strings are ordinary document text, never PDF names.
    return False
