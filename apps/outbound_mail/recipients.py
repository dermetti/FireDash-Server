"""Provider-independent, fresh report-recipient authorization."""

from dataclasses import dataclass, field
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.organizations.models import Department
from apps.outbound_mail.services import is_department_recipient_allowed
from apps.personnel.models import Person


class ReportRecipientError(ValueError):
    """A stable, deliberately non-disclosing recipient-policy failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Report recipient authorization failed ({code}).")


@dataclass(frozen=True, repr=False)
class AdmittedReportRecipient:
    """Current recipient resolution, with the email hidden from normal diagnostics."""

    personnel_id: UUID
    email: str = field(repr=False)

    def __repr__(self) -> str:
        return f"AdmittedReportRecipient(personnel_id={self.personnel_id!r}, email=<redacted>)"

    __str__ = __repr__


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
        raise ReportRecipientError("recipient_not_authorized")
    if (
        not person.active
        or person.lifecycle_status != Person.LifecycleStatus.ACTIVE
        or not person.incident_commander_eligible
        or person.email_verified_at is None
    ):
        raise ReportRecipientError("recipient_not_authorized")
    email = _normalized_valid_email(person.incident_commander_email)
    if email is None:
        raise ReportRecipientError("recipient_email_unavailable")
    if not is_department_recipient_allowed(department=department, recipient_email=email):
        raise ReportRecipientError("recipient_domain_not_allowed")
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
