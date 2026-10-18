import io
from datetime import timedelta

import pikepdf
import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.outbound_mail.report_delivery import (
    REPORT_ATTACHMENT_FILENAME,
    REPORT_BODY,
    REPORT_SUBJECT,
    ReportDeliveryCode,
    deliver_outbound_report,
)
from apps.outbound_mail.resolution import DepartmentMailProviderResolution
from apps.outbound_mail.runtime import (
    MailAddress,
    MailSendResult,
    MessageRejectedError,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from apps.personnel.models import Person
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def delivery_context(db):
    now = timezone.now()
    admin = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    tablet = Tablet.objects.create(
        department=department, display_name="Tablet", status=Tablet.Status.ACTIVE
    )
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash="a" * 64,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"key",
        hpke_ciphersuite="suite",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    person = Person.objects.create(
        department=department,
        display_name="Commander",
        incident_commander_eligible=True,
        incident_commander_email="commander@example.test",
        email_verified_at=now,
    )
    return department, installation, person


def _encrypted_pdf() -> bytes:
    document = pikepdf.Pdf.new()
    document.add_blank_page()
    output = io.BytesIO()
    document.save(
        output,
        encryption=pikepdf.Encryption(user="pdf-password", owner="owner-password", R=6),
    )
    return output.getvalue()


class FakeProvider:
    provider_id = "FAKE"

    def __init__(self, outcome=None):
        self.sender_identity = MailAddress(display_name="FireDash", email="sender@example.test")
        self.outcome = outcome or MailSendResult(provider=self.provider_id)
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _usable(provider):
    return DepartmentMailProviderResolution(provider=provider, provider_id=provider.provider_id)


def test_delivery_admits_then_sends_one_generic_canonical_message(delivery_context, monkeypatch):
    department, installation, person = delivery_context
    provider = FakeProvider()
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery.resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )
    encrypted_pdf = _encrypted_pdf()

    outcome = deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=person.id,
        pdf=encrypted_pdf,
        filename="tablet-private-name.pdf",
    )

    assert outcome == type(outcome)(
        code=ReportDeliveryCode.DELIVERED,
        delivered=True,
        recipient_email="commander@example.test",
    )
    assert len(provider.messages) == 1
    message = provider.messages[0]
    assert message.sender == provider.sender_identity
    assert message.recipient.email == "commander@example.test"
    assert message.subject == REPORT_SUBJECT and message.body == REPORT_BODY
    assert message.attachments[0].filename == REPORT_ATTACHMENT_FILENAME
    assert message.attachments[0].content == encrypted_pdf
    assert "tablet-private-name" not in message.attachments[0].filename

    event = AuditEvent.objects.get(action="outbound_mail.report_delivery_attempted")
    assert event.department_id == department.id
    assert event.actor_installation_uuid == installation.installation_uuid
    assert event.target_uuid == person.id
    assert event.metadata == {"outcome": ReportDeliveryCode.DELIVERED}
    rendered = f"{event.metadata!r} {event!r}"
    assert "commander@example.test" not in rendered
    assert "tablet-private-name" not in rendered
    assert encrypted_pdf[:40].decode("latin1") not in rendered


def test_fresh_recipient_check_and_unavailable_provider_prevent_send(delivery_context, monkeypatch):
    department, installation, person = delivery_context
    provider = FakeProvider()
    from apps.outbound_mail import report_delivery

    original_admission = report_delivery.admit_outbound_report

    def admission_then_deactivate(**kwargs):
        admitted = original_admission(**kwargs)
        Person.objects.filter(pk=person.id).update(
            active=False,
            lifecycle_status=Person.LifecycleStatus.DEPARTED,
            departed_at=timezone.now(),
        )
        return admitted

    monkeypatch.setattr(report_delivery, "admit_outbound_report", admission_then_deactivate)
    monkeypatch.setattr(
        report_delivery,
        "resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )
    outcome = deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=person.id,
        pdf=_encrypted_pdf(),
        filename="r.pdf",
    )
    assert outcome.code == ReportDeliveryCode.RECIPIENT_NOT_AUTHORIZED
    assert not provider.messages
    assert not AuditEvent.objects.filter(action="outbound_mail.report_delivery_attempted").exists()

    unavailable = DepartmentMailProviderResolution(
        provider=None, unavailable_reason="department_disabled"
    )
    monkeypatch.setattr(report_delivery, "admit_outbound_report", original_admission)
    monkeypatch.setattr(
        report_delivery, "resolve_department_mail_provider", lambda *, department: unavailable
    )
    person.active = True
    person.lifecycle_status = Person.LifecycleStatus.ACTIVE
    person.departed_at = None
    person.save(update_fields=("active", "lifecycle_status", "departed_at"))
    outcome = deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=person.id,
        pdf=_encrypted_pdf(),
        filename="r.pdf",
    )
    assert outcome.code == ReportDeliveryCode.DELIVERY_UNAVAILABLE
    assert not provider.messages


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProviderUnavailableError(), ReportDeliveryCode.PROVIDER_UNAVAILABLE),
        (MessageRejectedError(), ReportDeliveryCode.MESSAGE_REJECTED),
        (ProviderConfigurationError(), ReportDeliveryCode.PROVIDER_CONFIGURATION),
        (TimeoutError(), ReportDeliveryCode.PROVIDER_UNAVAILABLE),
        (RuntimeError("ambiguous provider response"), ReportDeliveryCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_provider_failures_are_sanitized_audited_and_never_retried(
    delivery_context, monkeypatch, failure, expected
):
    _department, installation, person = delivery_context
    provider = FakeProvider(outcome=failure)
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery.resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )

    outcome = deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=person.id,
        pdf=_encrypted_pdf(),
        filename="r.pdf",
    )
    assert outcome.code == expected and not outcome.delivered
    assert len(provider.messages) == 1
    event = AuditEvent.objects.get(action="outbound_mail.report_delivery_attempted")
    assert event.metadata == {"outcome": expected}
    assert "ambiguous" not in repr(event.metadata)


def test_delivery_does_not_create_report_or_attachment_state(delivery_context, monkeypatch):
    _department, installation, person = delivery_context
    provider = FakeProvider()
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery.resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )
    before = set(AuditEvent.objects.values_list("id", flat=True))
    deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=person.id,
        pdf=_encrypted_pdf(),
        filename="r.pdf",
    )
    created = AuditEvent.objects.exclude(id__in=before)
    assert created.count() == 1
    assert created.get().action == "outbound_mail.report_delivery_attempted"
