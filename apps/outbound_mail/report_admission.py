"""Provider-independent, side-effect-free report email admission.

This is deliberately before provider resolution and transport.  Its return
value holds the in-memory attachment needed by a later delivery boundary, but
never has a useful default string representation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import BinaryIO
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from pypdf import PdfReader
from pypdf._encryption import PasswordType
from pypdf.errors import PdfReadError, PyPdfError
from pypdf.generic import DictionaryObject

from apps.organizations.models import Department
from apps.outbound_mail.services import is_department_recipient_allowed
from apps.personnel.models import Person


class ReportAdmissionError(ValueError):
    """A stable, deliberately non-disclosing report-admission failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Outbound report admission failed ({code}).")


@dataclass(frozen=True, repr=False)
class AdmittedReportRecipient:
    """Current recipient resolution, with the email hidden from normal diagnostics."""

    personnel_id: UUID
    email: str = field(repr=False)

    def __repr__(self) -> str:
        return f"AdmittedReportRecipient(personnel_id={self.personnel_id!r}, email=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class AdmittedReport:
    """Ephemeral validated report material; it is never a persistence model."""

    recipient: AdmittedReportRecipient
    filename: str = field(repr=False)
    pdf_bytes: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            "AdmittedReport("
            f"recipient={self.recipient!r}, filename=<redacted>, "
            f"pdf_bytes=<redacted:{len(self.pdf_bytes)}>)"
        )

    __str__ = __repr__


def admit_outbound_report(
    *,
    department: Department,
    recipient_personnel_id: UUID,
    pdf: bytes | BinaryIO,
    filename: str,
) -> AdmittedReport:
    """Validate a report request from already-authenticated tablet context.

    ``department`` must originate from the authenticated installation/tablet,
    never a tablet-provided department selector.  The function makes no writes
    and intentionally does not resolve a provider.
    """
    recipient = _resolve_recipient(department=department, personnel_id=recipient_personnel_id)
    pdf_bytes = _read_bounded_pdf(pdf)
    _inspect_aes256_password_pdf(pdf_bytes)
    return AdmittedReport(
        recipient=recipient,
        filename=_validated_filename(filename),
        pdf_bytes=pdf_bytes,
    )


def _resolve_recipient(*, department: Department, personnel_id: UUID) -> AdmittedReportRecipient:
    # Scope in the lookup itself so a foreign UUID and an absent UUID have the
    # same public outcome.
    person = (
        Person.objects.filter(pk=personnel_id, department_id=department.id)
        .only(
            "id",
            "active",
            "lifecycle_status",
            "incident_commander_eligible",
            "incident_commander_email",
            "email_verified_at",
        )
        .first()
    )
    if person is None:
        raise ReportAdmissionError("recipient_not_authorized")
    if (
        not person.active
        or person.lifecycle_status != Person.LifecycleStatus.ACTIVE
        or not person.incident_commander_eligible
        or person.email_verified_at is None
    ):
        raise ReportAdmissionError("recipient_not_authorized")
    email = _normalized_valid_email(person.incident_commander_email)
    if email is None:
        raise ReportAdmissionError("recipient_email_unavailable")
    if not is_department_recipient_allowed(department=department, recipient_email=email):
        raise ReportAdmissionError("recipient_domain_not_allowed")
    return AdmittedReportRecipient(personnel_id=person.id, email=email)


def resolve_report_recipient(
    *, department: Department, recipient_personnel_id: UUID
) -> AdmittedReportRecipient:
    """Fresh server-side recipient/policy authorization for report delivery."""
    return _resolve_recipient(department=department, personnel_id=recipient_personnel_id)


def _normalized_valid_email(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    normalized = value.casefold()
    try:
        validate_email(normalized)
    except ValidationError:
        return None
    return normalized


def _read_bounded_pdf(pdf: bytes | BinaryIO) -> bytes:
    limit = settings.OUTBOUND_MAIL_REPORT_ATTACHMENT_MAX_BYTES
    if not isinstance(limit, int) or limit <= 0:
        raise ReportAdmissionError("attachment_limit_unavailable")
    if isinstance(pdf, bytes):
        if len(pdf) > limit:
            raise ReportAdmissionError("attachment_too_large")
        return pdf
    if not hasattr(pdf, "read"):
        raise ReportAdmissionError("invalid_attachment")
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = pdf.read(min(64 * 1024, limit + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ReportAdmissionError("invalid_attachment")
        size += len(chunk)
        if size > limit:
            raise ReportAdmissionError("attachment_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _validated_filename(filename: str) -> str:
    # Never put this caller-provided value in an exception. It is only delivery
    # metadata; the bytes and parsed-PDF inspection establish document type.
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise ReportAdmissionError("invalid_attachment")
    return filename


def _inspect_aes256_password_pdf(pdf_bytes: bytes) -> None:
    """Accept only parsed, password-required AES-256 revision-6 PDFs.

    ``pypdf==6.18.0`` parses the encryption dictionary without authenticating a
    password.  The small private-API use below is isolated deliberately:
    pypdf's effective ``Encryption`` fields account for crypt-filter defaults
    (notably an omitted ``/EFF``), which the raw parsed dictionary alone cannot
    establish. Tests pin this dependency contract fail-closed.
    """
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ReportAdmissionError("invalid_pdf")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
    except (PyPdfError, ValueError, TypeError, OverflowError):
        raise ReportAdmissionError("malformed_pdf") from None
    if not reader.is_encrypted:
        raise ReportAdmissionError("unsupported_pdf_encryption")
    try:
        empty_password_result = reader.decrypt("")
    except (PyPdfError, ValueError, TypeError, OverflowError):
        raise ReportAdmissionError("unsupported_pdf_encryption") from None
    if empty_password_result is not PasswordType.NOT_DECRYPTED:
        raise ReportAdmissionError("unsupported_pdf_encryption")
    if not _is_password_required_aes256_revision6(reader):
        raise ReportAdmissionError("unsupported_pdf_encryption")


def _is_password_required_aes256_revision6(reader: PdfReader) -> bool:
    """Validate pypdf's parsed and effective encryption representation."""
    try:
        encrypt = reader.trailer["/Encrypt"]
        if not isinstance(encrypt, DictionaryObject):
            return False
        crypt_filters = encrypt.get("/CF")
        if not isinstance(crypt_filters, DictionaryObject):
            return False
        stream_filter = encrypt.get("/StmF")
        string_filter = encrypt.get("/StrF")
        if not isinstance(stream_filter, str) or stream_filter != string_filter:
            return False
        crypt_filter = crypt_filters.get(stream_filter)
        if not isinstance(crypt_filter, DictionaryObject):
            return False
        # pypdf's effective encryption fields are deliberately checked as well:
        # EFF correctly applies the standard default when /EFF is omitted.
        encryption = reader._encryption
        return (
            encrypt.get("/V") == 5
            and encrypt.get("/R") == 6
            and encrypt.get("/Length") == 256
            and crypt_filter.get("/CFM") == "/AESV3"
            and crypt_filter.get("/Length") in (32, 256)
            and encryption.V == 5
            and encryption.R == 6
            and encryption.Length == 256
            and encryption.StmF == "/AESV3"
            and encryption.StrF == "/AESV3"
            and encryption.EFF == "/AESV3"
        )
    except (AttributeError, KeyError, TypeError, ValueError, PdfReadError):
        return False
