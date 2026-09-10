"""Provider-independent, side-effect-free report email admission.

This is deliberately before provider resolution and transport.  Its return
value holds the in-memory attachment needed by a later delivery boundary, but
never has a useful default string representation.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import BinaryIO
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

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
    # metadata; the bytes and qpdf inspection establish document type.
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise ReportAdmissionError("invalid_attachment")
    return filename


# pikepdf/qpdf confirms that the document is password protected without trying
# passwords. qpdf's Python API intentionally does not expose encryption details
# before authentication, so require the unambiguous AES-256 revision-6 envelope
# markers as an additional fail-closed structural gate. This examines no content
# streams and never decrypts or rewrites the document.
_AES256_R6_MARKERS = (
    re.compile(rb"/R\s+6(?:\s|/|>>)", re.ASCII),
    re.compile(rb"/V\s+5(?:\s|/|>>)", re.ASCII),
    re.compile(rb"/Length\s+256(?:\s|/|>>)", re.ASCII),
    re.compile(rb"/AESV3(?:\s|/|>>)", re.ASCII),
)


def _inspect_aes256_password_pdf(pdf_bytes: bytes) -> None:
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ReportAdmissionError("invalid_pdf")
    try:
        import pikepdf

        # Opening with only the empty password is intentionally not password
        # guessing. A PasswordError is qpdf's mature parser confirmation that a
        # non-empty document password protects the PDF.
        with pikepdf.open(io.BytesIO(pdf_bytes), password="", attempt_recovery=False):
            pass
    except pikepdf.PasswordError:
        if all(marker.search(pdf_bytes) for marker in _AES256_R6_MARKERS):
            return
        raise ReportAdmissionError("unsupported_pdf_encryption") from None
    except Exception:
        raise ReportAdmissionError("malformed_pdf") from None
    raise ReportAdmissionError("pdf_password_required")
