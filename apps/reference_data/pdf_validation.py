import hashlib
from collections.abc import Mapping
from pathlib import Path

from django.conf import settings


class PdfValidationError(ValueError):
    pass


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


def validate_pdf(path: Path, *, original_filename: str | None = None) -> tuple[int, int, str]:
    if original_filename is not None and not original_filename.casefold().endswith(".pdf"):
        raise PdfValidationError("Only PDF uploads are accepted.")
    if path.stat().st_size > settings.MAX_PDF_INPUT_BYTES and original_filename is not None:
        raise PdfValidationError("PDF input exceeds the configured size limit.")
    if path.stat().st_size > settings.MAX_PDF_OUTPUT_BYTES and original_filename is None:
        raise PdfValidationError("Sanitized PDF exceeds the configured size limit.")
    if not path.read_bytes()[:5].startswith(b"%PDF-"):
        raise PdfValidationError("Upload does not have a PDF signature.")
    _validate_mime(path)
    return _inspect_pdf(path)


def _validate_mime(path: Path) -> None:
    try:
        import magic
    except ImportError as error:
        raise PdfValidationError("python-magic is required for PDF validation.") from error
    try:
        mime_type = magic.from_file(str(path), mime=True)
    except Exception as error:
        raise PdfValidationError("PDF MIME type could not be validated.") from error
    if mime_type != "application/pdf":
        raise PdfValidationError("Upload MIME type is not application/pdf.")


def _inspect_pdf(path: Path) -> tuple[int, int, str]:
    try:
        import pikepdf

        with pikepdf.open(path) as document:
            if document.is_encrypted:
                raise PdfValidationError("Encrypted PDFs are not accepted.")
            page_count = len(document.pages)
            if page_count > settings.MAX_PDF_PAGES:
                raise PdfValidationError("PDF page limit exceeded.")
            if _contains_prohibited_content(document.Root):
                raise PdfValidationError("PDF contains prohibited active or external content.")
    except PdfValidationError:
        raise
    except Exception as error:
        raise PdfValidationError("PDF is malformed or cannot be parsed.") from error
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
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in PROHIBITED_NAMES or _contains_prohibited_content(item, seen):
                return True
    elif isinstance(value, list | tuple):
        return any(_contains_prohibited_content(item, seen) for item in value)
    return False
