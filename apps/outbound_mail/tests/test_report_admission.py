import io
import warnings

import pikepdf
import pypdf
import pytest
from django.test import override_settings
from django.utils import timezone
from pypdf import PdfReader
from pypdf._encryption import PasswordType

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.outbound_mail.report_admission import (
    ReportAdmissionError,
    _is_password_required_aes256_revision6,
    admit_outbound_report,
)
from apps.outbound_mail.services import set_department_recipient_policy
from apps.personnel.models import Person


@pytest.fixture
def report_scope(db):
    admin = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=admin)
    other_department = Department.objects.create(name="Other", short_code="other", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    person = Person.objects.create(
        department=department, display_name="Commander", incident_commander_eligible=True,
        incident_commander_email="commander@example.test", email_verified_at=timezone.now(),
    )
    return admin, department, other_department, person


def _encrypted_pdf(*, revision=6, aes=True, user="report-password") -> bytes:
    document = pikepdf.Pdf.new()
    document.add_blank_page()
    output = io.BytesIO()
    document.save(output, encryption=pikepdf.Encryption(
        user=user, owner="owner-password", R=revision, aes=aes, metadata=revision >= 4
    ))
    return output.getvalue()


def _plain_pdf() -> bytes:
    document = pikepdf.Pdf.new()
    document.add_blank_page()
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _admit(department, person, payload, filename="r.pdf"):
    return admit_outbound_report(
        department=department, recipient_personnel_id=person.id, pdf=payload, filename=filename
    )


def test_admission_resolves_only_current_eligible_scoped_personnel(report_scope):
    _admin, department, other_department, person = report_scope
    attachment = _encrypted_pdf()
    admitted = _admit(department, person, attachment)
    assert admitted.recipient.personnel_id == person.id
    assert admitted.recipient.email == "commander@example.test"
    assert admitted.pdf_bytes == attachment
    foreign = Person.objects.create(
        department=other_department,
        display_name="Other commander",
        incident_commander_eligible=True,
        incident_commander_email="other@example.test", email_verified_at=timezone.now(),
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
                filename="r.pdf",
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
    assert _admit(department, person, attachment).recipient.email == "commander@example.test"
    person.incident_commander_email = "commander@sub.example.test"
    person.save(update_fields=("incident_commander_email",))
    with pytest.raises(ReportAdmissionError) as error:
        _admit(department, person, attachment)
    assert error.value.code == "recipient_domain_not_allowed"


@override_settings(OUTBOUND_MAIL_REPORT_ATTACHMENT_MAX_BYTES=4096)
def test_only_password_required_aes256_r6_pdfs_are_accepted(report_scope):
    _admin, department, _other_department, person = report_scope
    strong = _encrypted_pdf(revision=6)
    assert _admit(department, person, io.BytesIO(strong)).pdf_bytes == strong
    rc4 = _encrypted_pdf(revision=2, aes=False)
    aes128 = _encrypted_pdf(revision=4)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        aes256_r5 = _encrypted_pdf(revision=5)
    empty_password = _encrypted_pdf(revision=6, user="")
    for payload in (_plain_pdf(), rc4, aes128, aes256_r5, empty_password):
        with pytest.raises(ReportAdmissionError) as error:
            _admit(department, person, payload)
        assert error.value.code == "unsupported_pdf_encryption"
    invalid_cases = (
        (b"not a PDF", "invalid_pdf"),
        (strong[:-20], "malformed_pdf"),
        (strong + b"x" * 4096, "attachment_too_large"),
    )
    for payload, code in invalid_cases:
        with pytest.raises(ReportAdmissionError) as error:
            _admit(department, person, payload)
        assert error.value.code == code


def test_parsed_pypdf_contract_rejects_spoofed_and_incomplete_encryption(report_scope, monkeypatch):
    _admin, department, _other_department, person = report_scope
    assert pypdf.__version__ == "6.18.0"
    weak_with_markers = _encrypted_pdf(revision=4) + b"\n/R 6 /Length 256 /AESV3\n"
    with pytest.raises(ReportAdmissionError) as error:
        _admit(department, person, weak_with_markers)
    assert error.value.code == "unsupported_pdf_encryption"
    reader = PdfReader(io.BytesIO(_encrypted_pdf()))
    assert reader.decrypt("") is PasswordType.NOT_DECRYPTED
    assert _is_password_required_aes256_revision6(reader) is True
    monkeypatch.setattr(reader._encryption, "EFF", "/Identity")
    assert _is_password_required_aes256_revision6(reader) is False
    reader = PdfReader(io.BytesIO(_encrypted_pdf()))
    reader.trailer["/Encrypt"].pop("/CF")
    assert _is_password_required_aes256_revision6(reader) is False


def test_admission_values_and_errors_redact_all_caller_sensitive_material(report_scope):
    _admin, department, _other_department, person = report_scope
    payload = _encrypted_pdf()
    admitted = _admit(department, person, payload, "private-report.pdf")
    rendered = f"{admitted!r} {admitted!s} {admitted.recipient!r}"
    assert "private-report.pdf" not in rendered
    assert "commander@example.test" not in rendered
    assert payload[:40].decode("latin1") not in rendered
    with pytest.raises(ReportAdmissionError) as error:
        _admit(department, person, b"private document contents", "private-report.pdf")
    assert "private" not in str(error.value)


def test_admission_is_observational_and_creates_no_audit_or_report_state(report_scope):
    _admin, department, _other_department, person = report_scope
    before_events = AuditEvent.objects.count()
    _admit(department, person, _encrypted_pdf())
    assert AuditEvent.objects.count() == before_events
