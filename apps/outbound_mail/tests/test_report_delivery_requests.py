import hashlib
import hmac
import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.outbound_mail.models import TabletReportDeliveryRequest
from apps.outbound_mail.report_delivery import ReportDeliveryCode, ReportDeliveryOutcome
from apps.outbound_mail.report_delivery_requests import submit_tablet_report_delivery
from apps.outbound_mail.resolution import DepartmentMailProviderResolution
from apps.outbound_mail.runtime import MailAddress, MailSendResult
from apps.personnel.models import Person
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def request_context(db):
    now = timezone.now()
    user = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=user)
    tablet = Tablet.objects.create(department=department, display_name="Tablet")
    credential = "tablet-delivery-credential"
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid4(),
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
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
    return installation, person, credential


def test_first_submission_calls_delivery_once_then_replays_terminal_outcome(
    request_context, monkeypatch
):
    installation, person, _credential = request_context
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return ReportDeliveryOutcome(code=ReportDeliveryCode.DELIVERED, delivered=True)

    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery_requests.deliver_outbound_report", deliver
    )
    request_id = uuid4()
    first = submit_tablet_report_delivery(
        installation=installation,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
        pdf=b"encrypted-pdf",
    )
    second = submit_tablet_report_delivery(
        installation=installation,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
        pdf=b"replacement-is-never-read",
    )
    assert first.state == second.state == TabletReportDeliveryRequest.State.SUCCESS
    assert first.code == second.code == ReportDeliveryCode.DELIVERED
    assert len(calls) == 1
    stored = TabletReportDeliveryRequest.objects.get()
    assert stored.recipient_personnel_id == person.id
    assert not hasattr(stored, "pdf") and stored.result_code == ReportDeliveryCode.DELIVERED


def test_unknown_processing_and_conflicting_replays_never_send(request_context, monkeypatch):
    installation, person, _credential = request_context
    request_id = uuid4()
    TabletReportDeliveryRequest.objects.create(
        installation=installation,
        department=installation.tablet.department,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
    )
    delivery = pytest.MonkeyPatch()
    calls = []
    delivery.setattr(
        "apps.outbound_mail.report_delivery_requests.deliver_outbound_report",
        lambda **kwargs: calls.append(kwargs),
    )
    try:
        processing = submit_tablet_report_delivery(
            installation=installation,
            delivery_request_id=request_id,
            recipient_personnel_id=person.id,
            pdf=b"ignored",
        )
        conflict = submit_tablet_report_delivery(
            installation=installation,
            delivery_request_id=request_id,
            recipient_personnel_id=uuid4(),
            pdf=b"ignored",
        )
    finally:
        delivery.undo()
    assert (processing.state, processing.code) == ("UNKNOWN", "delivery_indeterminate")
    assert (conflict.state, conflict.code) == ("CONFLICT", "idempotency_conflict")
    assert not calls


def test_provider_unavailable_is_terminal_unknown_without_resend(request_context, monkeypatch):
    installation, person, _credential = request_context
    calls = []
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery_requests.deliver_outbound_report",
        lambda **kwargs: calls.append(kwargs)
        or ReportDeliveryOutcome(code=ReportDeliveryCode.PROVIDER_UNAVAILABLE),
    )
    request_id = uuid4()
    first = submit_tablet_report_delivery(
        installation=installation,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
        pdf=b"x",
    )
    replay = submit_tablet_report_delivery(
        installation=installation,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
        pdf=b"x",
    )
    assert (first.state, replay.state) == ("UNKNOWN", "UNKNOWN")
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_claims_send_at_most_once(request_context, monkeypatch):
    """Use independent PostgreSQL connections, not Django's in-memory test helpers."""
    installation, person, _credential = request_context
    request_id = uuid4()
    barrier = Barrier(2)
    calls: list[object] = []
    lock = Lock()

    def deliver(**kwargs):
        with lock:
            calls.append(kwargs)
        return ReportDeliveryOutcome(code=ReportDeliveryCode.DELIVERED, delivered=True)

    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery_requests.deliver_outbound_report", deliver
    )

    def submit():
        from django.db import connection

        connection.close()
        try:
            barrier.wait(timeout=10)
            fresh_installation = AppInstallation.objects.get(pk=installation.pk)
            return submit_tablet_report_delivery(
                installation=fresh_installation,
                delivery_request_id=request_id,
                recipient_personnel_id=person.id,
                pdf=b"never-persisted",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert len(calls) == 1
    assert {(result.state, result.code) for result in results} == {
        (TabletReportDeliveryRequest.State.SUCCESS, ReportDeliveryCode.DELIVERED),
        ("UNKNOWN", "delivery_indeterminate"),
    }
    assert TabletReportDeliveryRequest.objects.filter(
        installation=installation, delivery_request_id=request_id
    ).count() == 1
    replay = submit_tablet_report_delivery(
        installation=installation,
        delivery_request_id=request_id,
        recipient_personnel_id=person.id,
        pdf=b"ignored",
    )
    assert (replay.state, replay.code) == (
        TabletReportDeliveryRequest.State.SUCCESS,
        ReportDeliveryCode.DELIVERED,
    )
    assert len(calls) == 1


def test_authenticated_multipart_endpoint_uses_idempotency_service(request_context, monkeypatch):
    installation, person, credential = request_context
    calls = []
    monkeypatch.setattr(
        "apps.tablets.api.submit_tablet_report_delivery",
        lambda **kwargs: calls.append(kwargs)
        or type("Result", (), {"state": "SUCCESS", "code": "delivered"})(),
    )
    request_id = uuid4()
    client = Client()
    response = client.post(
        "/api/v1/tablet/report-delivery",
        {
            "delivery_request_id": str(request_id),
            "recipient_personnel_id": str(person.id),
            "pdf": SimpleUploadedFile("client-name.pdf", b"encrypted-pdf", "application/pdf"),
        },
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )
    assert response.status_code == 200
    assert response.json() == {"state": "SUCCESS", "code": "delivered"}
    assert len(calls) == 1


def _encrypted_pdf() -> bytes:
    import pikepdf

    document = pikepdf.Pdf.new()
    document.add_blank_page()
    output = io.BytesIO()
    document.save(output, encryption=pikepdf.Encryption(user="password", owner="owner", R=6))
    return output.getvalue()


class _EndpointProvider:
    provider_id = "FAKE"
    sender_identity = MailAddress(display_name="FireDash", email="sender@example.test")

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return MailSendResult(provider=self.provider_id)


def _usable(provider):
    return DepartmentMailProviderResolution(provider=provider, provider_id="FAKE")


def _qpdf_payload(encrypted: bool) -> bytes:
    return json.dumps(
        {"encrypt": {"encrypted": encrypted, "userpasswordmatched": False,
                      "ownerpasswordmatched": False,
                      "parameters": {"R": 6, "V": 5, "bits": 256, "method": "AESv3",
                                     "stringmethod": "AESv3", "streammethod": "AESv3",
                                     "filemethod": "AESv3"}}}
    ).encode()


def _endpoint_post(client, credential, request_id, recipient_id, content, filename="client.pdf"):
    return client.post(
        "/api/v1/tablet/report-delivery",
        {"delivery_request_id": str(request_id), "recipient_personnel_id": str(recipient_id),
         "pdf": SimpleUploadedFile(filename, content, "application/pdf")},
        HTTP_AUTHORIZATION=f"Bearer {credential}",
    )


def test_endpoint_admission_failures_use_existing_admission_without_send(
    request_context, monkeypatch
):
    _installation, person, credential = request_context
    provider = _EndpointProvider()
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery.resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )

    def qpdf(*args, input, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0 if input.startswith(b"%PDF-") else 2,
            stdout=_qpdf_payload(False) if input.startswith(b"%PDF-") else b"",
        )

    monkeypatch.setattr("apps.outbound_mail.report_admission.subprocess.run", qpdf)
    client = Client()
    plain = _endpoint_post(client, credential, uuid4(), person.id, b"%PDF-1.7\nplain")
    malformed = _endpoint_post(client, credential, uuid4(), person.id, b"not-a-pdf")
    with override_settings(OUTBOUND_MAIL_REPORT_ATTACHMENT_MAX_BYTES=8):
        oversized = _endpoint_post(client, credential, uuid4(), person.id, b"x" * 9)
    assert plain.json() == {"state": "FAILED", "code": "unsupported_pdf_encryption"}
    assert malformed.json() == {"state": "FAILED", "code": "invalid_pdf"}
    assert oversized.json() == {"state": "FAILED", "code": "attachment_too_large"}
    assert not provider.messages


def test_endpoint_replay_conflict_and_cross_department_are_fail_closed(
    request_context, monkeypatch
):
    installation, person, credential = request_context
    provider = _EndpointProvider()
    monkeypatch.setattr(
        "apps.outbound_mail.report_delivery.resolve_department_mail_provider",
        lambda *, department: _usable(provider),
    )
    monkeypatch.setattr(
        "apps.outbound_mail.report_admission.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=0, stdout=_qpdf_payload(True)
        ),
    )
    other = Department.objects.create(
        name="Other", short_code="other", created_by=installation.tablet.department.created_by
    )
    foreign = Person.objects.create(
        department=other,
        display_name="Foreign",
        incident_commander_eligible=True,
        incident_commander_email="foreign@example.test",
        email_verified_at=timezone.now(),
    )
    client, request_id, payload = Client(), uuid4(), _encrypted_pdf()
    first = _endpoint_post(client, credential, request_id, person.id, payload, "private.pdf")
    replay = _endpoint_post(client, credential, request_id, person.id, payload, "replacement.pdf")
    conflict = _endpoint_post(client, credential, request_id, foreign.id, payload)
    foreign_request = _endpoint_post(client, credential, uuid4(), foreign.id, payload)
    person.incident_commander_eligible = False
    person.save(update_fields=("incident_commander_eligible",))
    ineligible = _endpoint_post(client, credential, uuid4(), person.id, payload)
    assert first.json() == replay.json() == {"state": "SUCCESS", "code": "delivered"}
    assert conflict.json() == {"state": "CONFLICT", "code": "idempotency_conflict"}
    assert foreign_request.json() == {"state": "FAILED", "code": "recipient_not_authorized"}
    assert ineligible.json() == {"state": "FAILED", "code": "recipient_not_authorized"}
    assert len(provider.messages) == 1
    assert "foreign@example.test" not in foreign_request.content.decode()
