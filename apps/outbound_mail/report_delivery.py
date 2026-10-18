"""One-attempt, provider-neutral delivery of an admitted encrypted report PDF."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID

from apps.audit.services import record_event
from apps.outbound_mail.report_admission import (
    ReportAdmissionError,
    admit_outbound_report,
    resolve_report_recipient,
)
from apps.outbound_mail.resolution import resolve_department_mail_provider
from apps.outbound_mail.runtime import (
    MailAttachment,
    MailProviderError,
    MailSendResult,
    MessageRejectedError,
    OutboundMessage,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.tablets.models import AppInstallation

REPORT_SUBJECT = "FireDash report"
REPORT_BODY = "A FireDash report is attached."
REPORT_ATTACHMENT_FILENAME = "firedash-report.pdf"


class ReportDeliveryCode:
    DELIVERED = "delivered"
    RECIPIENT_NOT_AUTHORIZED = "recipient_not_authorized"
    RECIPIENT_EMAIL_UNAVAILABLE = "recipient_email_unavailable"
    RECIPIENT_DOMAIN_NOT_ALLOWED = "recipient_domain_not_allowed"
    INVALID_ATTACHMENT = "invalid_attachment"
    ATTACHMENT_TOO_LARGE = "attachment_too_large"
    INVALID_PDF = "invalid_pdf"
    UNSUPPORTED_PDF_ENCRYPTION = "unsupported_pdf_encryption"
    PDF_INSPECTION_UNAVAILABLE = "pdf_inspection_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MESSAGE_REJECTED = "message_rejected"
    PROVIDER_CONFIGURATION = "provider_configuration"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"


@dataclass(frozen=True, repr=False)
class ReportDeliveryOutcome:
    """Sanitized synchronous result; provider internals never escape this boundary."""

    code: str
    delivered: bool = False
    recipient_email: str | None = None

    def __repr__(self) -> str:
        return f"ReportDeliveryOutcome(code={self.code!r}, delivered={self.delivered})"

    __str__ = __repr__


def deliver_outbound_report(
    *,
    installation: AppInstallation,
    recipient_personnel_id: UUID,
    pdf: bytes | BinaryIO,
    filename: str,
) -> ReportDeliveryOutcome:
    """Admit, reauthorize, and synchronously attempt exactly one provider send.

    The caller supplies an already-authenticated installation context. This
    service neither retries nor stores report data; its sole write is a
    sanitized audit event after an actual provider ``send`` invocation.
    """
    department = installation.tablet.department
    try:
        admitted = admit_outbound_report(
            department=department,
            recipient_personnel_id=recipient_personnel_id,
            pdf=pdf,
            filename=filename,
        )
    except ReportAdmissionError as error:
        return ReportDeliveryOutcome(code=_admission_code(error.code))

    resolution = resolve_department_mail_provider(department=department)
    if not resolution.is_usable:
        return ReportDeliveryOutcome(code=ReportDeliveryCode.DELIVERY_UNAVAILABLE)

    # Resolve again immediately before building the send. The fresh email is
    # intentional: the admission-time value is never a recipient snapshot.
    try:
        recipient = resolve_report_recipient(
            department=department, recipient_personnel_id=admitted.recipient.personnel_id
        )
    except ReportAdmissionError as error:
        return ReportDeliveryOutcome(code=_admission_code(error.code))

    try:
        message = OutboundMessage(
            sender=resolution.provider.sender_identity,
            recipient=recipient_to_mail_address(recipient.email),
            subject=REPORT_SUBJECT,
            body=REPORT_BODY,
            attachments=(
                MailAttachment(
                    filename=REPORT_ATTACHMENT_FILENAME,
                    mime_type="application/pdf",
                    content=admitted.pdf_bytes,
                ),
            ),
        )
        result = resolution.provider.send(message)
        if not isinstance(result, MailSendResult):
            raise ProviderUnavailableError()
    except MailProviderError as error:
        outcome = ReportDeliveryOutcome(code=_provider_code(error))
    except Exception:
        # An unexpected or ambiguous provider exception has no safe retry.
        outcome = ReportDeliveryOutcome(code=ReportDeliveryCode.PROVIDER_UNAVAILABLE)
    else:
        outcome = ReportDeliveryOutcome(
            code=ReportDeliveryCode.DELIVERED,
            delivered=True,
            recipient_email=recipient.email,
        )

    record_event(
        action="outbound_mail.report_delivery_attempted",
        actor_installation_uuid=installation.installation_uuid,
        department=department,
        target_type="personnel_report_recipient",
        target_uuid=recipient.personnel_id,
        metadata={"outcome": outcome.code},
    )
    return outcome


def recipient_to_mail_address(email: str):
    """Avoid recipient display names; personnel identity is never mail content."""
    from apps.outbound_mail.runtime import MailAddress

    return MailAddress(display_name="FireDash recipient", email=email)


def _admission_code(code: str) -> str:
    allowed = {
        ReportDeliveryCode.RECIPIENT_NOT_AUTHORIZED,
        ReportDeliveryCode.RECIPIENT_EMAIL_UNAVAILABLE,
        ReportDeliveryCode.RECIPIENT_DOMAIN_NOT_ALLOWED,
        ReportDeliveryCode.INVALID_ATTACHMENT,
        ReportDeliveryCode.ATTACHMENT_TOO_LARGE,
        ReportDeliveryCode.INVALID_PDF,
        ReportDeliveryCode.UNSUPPORTED_PDF_ENCRYPTION,
        ReportDeliveryCode.PDF_INSPECTION_UNAVAILABLE,
    }
    return code if code in allowed else ReportDeliveryCode.INVALID_ATTACHMENT


def _provider_code(error: MailProviderError) -> str:
    if isinstance(error, ProviderUnavailableError):
        return ReportDeliveryCode.PROVIDER_UNAVAILABLE
    if isinstance(error, MessageRejectedError):
        return ReportDeliveryCode.MESSAGE_REJECTED
    if isinstance(error, ProviderConfigurationError):
        return ReportDeliveryCode.PROVIDER_CONFIGURATION
    return ReportDeliveryCode.PROVIDER_UNAVAILABLE
