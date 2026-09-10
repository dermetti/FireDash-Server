import io

import pikepdf
import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.outbound_mail.report_admission import ReportAdmissionError, admit_outbound_report
from apps.outbound_mail.services import set_department_recipient_policy
from apps.personnel.models import Person


@pytest.fixture
def report_scope(db):
    admin = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=admin)
    other_department = Department.objects.create(
        name="Other", short_code="other", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    person = Person.objects.create(
        department=department,
        display_name="Commander",
        incident_commander_eligible=True,
        incident_commander_email="commander@example.test",
        email_verified_at=timezone.now(),
    )
    return admin, department, other_department, person


def _encrypted_pdf(*, revision=6) -> bytes:
    document = pikepdf.Pdf.new()
    document.add_blank_page()
    output = io.BytesIO()
    document.save(
        output,
        encryption=pikepdf.Encryption(user="report-password", owner="owner-password", R=revision),
    )
    return output.getvalue()


def test_admission_resolves_only_current_eligible_scoped_personnel(report_scope):
    _admin, department, other_department, person = report_scope
    attachment = _encrypted_pdf()

    admitted = admit_outbound_report(
        department=department,
        recipient_personnel_id=person.id,
        pdf=attachment,
        filename="report.pdf",
    )
    assert admitted.recipient.personnel_id == person.id
    assert admitted.recipient.email == "commander@example.test"
    assert admitted.pdf_bytes == attachment

    foreign = Person.objects.create(
        department=other_department,
        display_name="Other commander",
        incident_commander_eligible=True,
        incident_commander_email="other@example.test",
        email_verified_at=timezone.now(),
    )
    for personnel_id in (foreign.id, person.id):
        if personnel_id == person.id:
            person.incident_commander_eligible = False
            person.save(update_fields=("incident_commander_eligible",))
        with pytest.raises(ReportAdmissionError) as error:
            admit_outbound_report(
                department=department,
                recipient_personnel_id=personnel_id,
                pdf=attachment,
                filename="report.pdf",
            )
        assert error.value.code == "recipient_not_authorized"

    person.incident_commander_eligible = True
    person.active = False
    person.lifecycle_status = Person.LifecycleStatus.DEPARTED
    person.departed_at = timezone.now()
    person.save(
        update_fields=("incident_commander_eligible", "active", "lifecycle_status", "departed_at")
    )
    with pytest.raises(ReportAdmissionError) as error:
        admit_outbound_report(
            department=department,
            recipient_personnel_id=person.id,
            pdf=attachment,
            filename="report.pdf",
        )
    assert error.value.code == "recipient_not_authorized"


def test_admission_uses_persisted_email_and_exact_domain_policy(report_scope):
    admin, department, _other_department, person = report_scope
    attachment = _encrypted_pdf()
    set_department_recipient_policy(
        actor=admin,
        department=department,
        restriction_enabled=True,
        approved_domains=["example.test"],
    )
    assert admit_outbound_report(
        department=department, recipient_personnel_id=person.id, pdf=attachment, filename="r.pdf"
    ).recipient.email == "commander@example.test"

    person.incident_commander_email = "commander@sub.example.test"
    person.save(update_fields=("incident_commander_email",))
    with pytest.raises(ReportAdmissionError) as error:
        admit_outbound_report(
            department=department,
            recipient_personnel_id=person.id,
            pdf=attachment,
            filename="r.pdf",
        )
    assert error.value.code == "recipient_domain_not_allowed"

    person.incident_commander_email = "not an email"
    person.save(update_fields=("incident_commander_email",))
    with pytest.raises(ReportAdmissionError) as error:
        admit_outbound_report(
            department=department,
            recipient_personnel_id=person.id,
            pdf=attachment,
            filename="r.pdf",
        )
    assert error.value.code == "recipient_email_unavailable"


@override_settings(OUTBOUND_MAIL_REPORT_ATTACHMENT_MAX_BYTES=4096)
def test_only_aes256_password_encrypted_bounded_pdfs_are_accepted(report_scope):
    _admin, department, _other_department, person = report_scope
    strong = _encrypted_pdf(revision=6)
    assert admit_outbound_report(
        department=department,
        recipient_personnel_id=person.id,
        pdf=io.BytesIO(strong),
        filename="r.pdf",
    ).pdf_bytes == strong

    plain = b"%PDF-1.7\nnot encrypted"
    weak = _encrypted_pdf(revision=4)
    for payload, code in (
        (plain, "malformed_pdf"),
        (weak, "unsupported_pdf_encryption"),
        (b"not a PDF", "invalid_pdf"),
        (strong[:-20], "malformed_pdf"),
        (strong + b"x" * 4096, "attachment_too_large"),
    ):
        with pytest.raises(ReportAdmissionError) as error:
            admit_outbound_report(
                department=department,
                recipient_personnel_id=person.id,
                pdf=payload,
                filename="r.pdf",
            )
        assert error.value.code == code


def test_admission_values_and_errors_redact_all_caller_sensitive_material(report_scope):
    _admin, department, _other_department, person = report_scope
    payload = _encrypted_pdf()
    admitted = admit_outbound_report(
        department=department,
        recipient_personnel_id=person.id,
        pdf=payload,
        filename="private-report.pdf",
    )
    rendered = f"{admitted!r} {admitted!s} {admitted.recipient!r}"
    assert "private-report.pdf" not in rendered
    assert "commander@example.test" not in rendered
    assert payload[:40].decode("latin1") not in rendered

    with pytest.raises(ReportAdmissionError) as error:
        admit_outbound_report(
            department=department,
            recipient_personnel_id=person.id,
            pdf=b"private document contents",
            filename="private-report.pdf",
        )
    assert "private" not in str(error.value)


def test_admission_is_observational_and_creates_no_audit_or_report_state(report_scope):
    _admin, department, _other_department, person = report_scope
    before_events = AuditEvent.objects.count()
    attachment = _encrypted_pdf()

    admit_outbound_report(
        department=department,
        recipient_personnel_id=person.id,
        pdf=io.BytesIO(attachment),
        filename="r.pdf",
    )

    assert AuditEvent.objects.count() == before_events
