import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import close_old_connections
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.audit.models import AuditEvent
from apps.outbound_mail.runtime import MailAddress, MailSendResult, MessageRejectedError
from apps.outbound_mail.tests.test_report_delivery_requests import request_context  # noqa: F401
from apps.report_transfer import transfers
from apps.report_transfer.contract import TransferError
from apps.report_transfer.models import ReportPortalSession, ReportSubmission, ReportTransfer
from apps.report_transfer.tests.test_submissions import (  # noqa: F401
    content,
    context,
    encrypt,
    package,
    post,
    report_key,
)

pytestmark = pytest.mark.django_db
API = "/api/v1/tablet/report-transfers"


@pytest.fixture
def mail(monkeypatch):
    provider = Mock()
    provider.sender_identity = MailAddress("FireDash", "sender@example.test")
    provider.send.return_value = MailSendResult(provider="test")
    resolution = Mock(is_usable=True, provider=provider)
    monkeypatch.setattr(
        transfers, "resolve_department_mail_provider", Mock(return_value=resolution)
    )
    return provider


@pytest.fixture
def submitted(context, report_key):  # noqa: F811
    installation, person, tablet, header = context
    payload = package(header)
    assert post(tablet, encrypt(report_key, header, payload)).status_code == 201
    return installation, person, tablet, ReportSubmission.objects.get(), payload


def issue(submitted, mail, *, request_id=None, recipient_id=None):
    _, _, tablet, row, _ = submitted
    response = tablet.post(
        API,
        data=json.dumps(
            {
                "transfer_request_id": str(request_id or uuid4()),
                "recipient_personnel_id": str(recipient_id or row.recipient_id),
            }
        ),
        content_type="application/json",
    )
    if response.status_code in (200, 201) and mail.send.called:
        url = re.search(r"https://\S+", mail.send.call_args.args[0].body).group()
    else:
        url = None
    return response, url


def browser_login(url, *, csrf=False):
    client = Client(enforce_csrf_checks=csrf)
    path = url.removeprefix("https://firedash.test")
    assert client.get(path, secure=True).status_code == 200
    data = {"csrfmiddlewaretoken": client.cookies["csrftoken"].value} if csrf else {}
    response = client.post(path + "redeem/", data, secure=True, HTTP_ORIGIN="https://testserver")
    return client, path, response


def test_issue_replay_neutral_mail_and_hash_only(submitted, mail, caplog):
    caplog.set_level(logging.DEBUG)
    request_id = uuid4()
    response, url = issue(submitted, mail, request_id=request_id)
    assert response.status_code == 201
    row = ReportTransfer.objects.get()
    assert row.mail_state == "SENT" and row.token_expires_at - row.issued_at == timedelta(
        minutes=60
    )
    token = url.split("/")[-2]
    assert len(token) == 43 and row.token_digest == transfers.digest(token)
    assert token not in json.dumps(response.json()) and token not in caplog.text
    assert token not in str(row.__dict__)
    message = mail.send.call_args.args[0]
    assert (
        not message.attachments and message.recipient.email == submitted[1].incident_commander_email
    )
    assert submitted[3].report_id.hex not in message.body
    assert submitted[1].display_name not in message.body
    replay, _ = issue(submitted, mail, request_id=request_id)
    assert replay.json() == response.json() and mail.send.call_count == 1
    assert ReportTransfer.objects.count() == 1


def test_scanner_get_then_exactly_one_login_and_used_revoke(submitted, mail):
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    scanner = Client()
    for _ in range(3):
        assert scanner.get(path).status_code == 200
    assert ReportTransfer.objects.get().consumed_at is None
    assert ReportPortalSession.objects.count() == 0
    browser, _, response = browser_login(url, csrf=True)
    assert response.status_code == 302 and response.url == "/reports/"
    assert response.cookies["sessionid"]["secure"]
    assert response.cookies["sessionid"]["httponly"]
    assert browser.get("/reports/", secure=True).status_code == 200
    assert browser.post(path + "redeem/", secure=True).status_code == 403  # CSRF rotated
    assert Client().post(path + "redeem/").status_code == 409
    used = scanner.get(path)
    assert "Dieser Zugangslink wurde bereits verwendet." in used.content.decode()
    assert "Zugang unwiderruflich sperren" in used.content.decode()
    assert scanner.get(path + "revoke/").status_code == 405
    confirm = scanner.post(path + "revoke/")
    assert confirm.status_code == 200
    assert ReportTransfer.objects.get().revoked_at is None
    assert scanner.post(path + "revoke/", {"confirm": "yes"}).status_code == 302
    assert browser.get("/reports/", secure=True).status_code == 403


def test_replacement_invalidates_old_session_and_old_token_cannot_revoke(submitted, mail):
    _, url = issue(submitted, mail)
    browser, oldpath, _ = browser_login(url)
    newer, newurl = issue(submitted, mail)
    assert newer.status_code == 201 and newurl != url
    assert browser.get("/reports/").status_code == 403
    assert Client().post(oldpath + "revoke/", {"confirm": "yes"}).status_code == 403
    current = ReportTransfer.objects.get(pk=newer.json()["transfer_id"])
    assert current.revoked_at is None
    newbrowser, _, result = browser_login(newurl)
    assert result.status_code == 302 and newbrowser.get("/reports/").status_code == 200
    assert Client().post(oldpath + "redeem/").status_code == 403


@pytest.mark.parametrize("used", [False, True])
def test_token_sixty_minute_boundary(submitted, mail, used):
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    if used:
        browser_login(url)
    ReportTransfer.objects.update(token_expires_at=timezone.now())
    client = Client()
    assert "Link nicht mehr gültig" in client.get(path).content.decode()
    assert client.post(path + "redeem/").status_code == 403
    assert client.post(path + "revoke/", {"confirm": "yes"}).status_code == 403
    assert ReportTransfer.objects.get().revoked_at is None


def test_unconsumed_token_cannot_revoke(submitted, mail):
    _, url = issue(submitted, mail)
    assert (
        Client()
        .post(url.removeprefix("https://firedash.test") + "revoke/", {"confirm": "yes"})
        .status_code
        == 403
    )
    assert ReportTransfer.objects.get().revoked_at is None


@pytest.mark.parametrize(
    "mutation",
    [
        "inactive",
        "unverified",
        "ineligible",
        "mailbox",
        "revoked_installation",
        "station",
        "department",
        "expired",
        "policy",
    ],
)
def test_live_authorization_on_each_portal_access(submitted, mail, mutation):
    installation, person, _, row, payload = submitted
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    if mutation == "inactive":
        person.active = False
        person.lifecycle_status = "DEPARTED"
        person.departed_at = timezone.now()
    elif mutation == "unverified":
        person.email_verified_at = None
    elif mutation == "ineligible":
        person.incident_commander_eligible = False
    elif mutation == "mailbox":
        person.incident_commander_email = "other@example.test"
    elif mutation == "revoked_installation":
        installation.status = installation.Status.REVOKED
        installation.revoked_at = timezone.now()
        installation.save()
    elif mutation in ("station", "department"):
        from apps.organizations.models import Department, Station

        other = (
            Department.objects.create(
                name="Other", short_code="other", created_by=row.department.created_by
            )
            if mutation == "department"
            else Station.objects.create(department=row.department, name="Other", short_code="O")
        )
        setattr(row, mutation, other)
        row.save()
    elif mutation == "expired":
        row.expires_at = timezone.now()
        row.save()
    elif mutation == "policy":
        from apps.outbound_mail.models import DepartmentRecipientPolicy

        DepartmentRecipientPolicy.objects.create(
            department=row.department, restriction_enabled=True
        )
    person.save()
    root = f"/reports/{row.report_id}/"
    photo = payload["attachments"][0]["id"]
    for path in (root, root + "pdf/", root + f"photos/{photo}/", root + f"photos/{photo}/preview/"):
        assert browser.get(path).status_code in (403, 404, 409, 410)


@pytest.mark.django_db(transaction=True)
def test_pdf_jpeg_preview_and_retention(submitted, mail):
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row, payload = submitted[3:]
    root = f"/reports/{row.report_id}/"
    assert "arbitrary.future-report" in browser.get(root).content.decode()
    photo = payload["attachments"][0]["id"]
    preview = browser.get(root + f"photos/{photo}/preview/")
    assert preview.status_code == 200 and preview["Content-Type"] == "image/jpeg"
    with Image.open(io.BytesIO(preview.content)) as image:
        assert image.width <= 640 and image.height <= 480
    row.refresh_from_db()
    assert row.first_download_at is None
    response = browser.get(root + "pdf/")
    row.refresh_from_db()
    assert row.first_download_at is None
    pdf = b"".join(response.streaming_content)
    response.close()
    assert pdf.startswith(b"%PDF-") and response["Content-Disposition"].startswith("attachment;")
    row.refresh_from_db()
    first, expiry = row.first_download_at, row.expires_at
    assert first and expiry <= first + timedelta(hours=48)
    jpeg = browser.get(root + f"photos/{photo}/")
    assert b"".join(jpeg.streaming_content).startswith(b"\xff\xd8")
    jpeg.close()
    row.refresh_from_db()
    assert row.first_download_at == first and row.expires_at == expiry


@pytest.mark.django_db(transaction=True)
def test_interrupted_stream_does_not_count(submitted, mail):
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row = submitted[3]
    response = browser.get(f"/reports/{row.report_id}/pdf/")
    stream = iter(response.streaming_content)
    assert next(stream).startswith(b"%PDF-")
    response.close()  # no normal exhaustion, even though this small PDF fits one chunk
    row.refresh_from_db()
    assert row.first_download_at is None


def test_csrf_headers_logout_and_idle_timeout(submitted, mail):
    _, url = issue(submitted, mail)
    browser, path, _ = browser_login(url, csrf=True)
    for address in (path, "/reports/", f"/reports/{submitted[3].report_id}/"):
        response = browser.get(address, secure=True)
        assert response["Cache-Control"] == "private, no-store"
        assert response["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
        assert response["X-Frame-Options"] == "DENY"
    assert browser.get("/reports/logout/").status_code == 405
    assert browser.post("/reports/logout/", secure=True).status_code == 403
    ReportPortalSession.objects.update(last_seen_at=timezone.now() - timedelta(minutes=30))
    assert browser.get("/reports/", secure=True).status_code == 403
    assert (
        browser.post(
            "/reports/logout/",
            {"csrfmiddlewaretoken": browser.cookies["csrftoken"].value},
            secure=True,
            HTTP_ORIGIN="https://testserver",
        ).status_code
        == 302
    )


def test_tablet_revoke_and_anonymous_foreign_uuid_denied(submitted, mail):
    response, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    _, _, tablet, row, payload = submitted
    root = f"/reports/{row.report_id}/"
    assert Client().get(root + "pdf/").status_code == 403
    assert browser.get(f"/reports/{uuid4()}/pdf/").status_code == 404
    assert browser.get(root + f"photos/{uuid4()}/").status_code == 404
    assert tablet.post(f"{API}/{uuid4()}/revoke").status_code == 404
    revoked = tablet.post(f"{API}/{response.json()['transfer_id']}/revoke")
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"
    assert browser.get(root + "pdf/").status_code == 403
    assert row.payload_key


@pytest.mark.parametrize("bad", ["email", "submission_ids", "missing", "foreign", "bad_uuid"])
def test_invalid_transfer_contract(submitted, mail, bad):
    _, _, tablet, row, _ = submitted
    data = {"transfer_request_id": str(uuid4()), "recipient_personnel_id": str(row.recipient_id)}
    if bad in ("email", "submission_ids"):
        data[bad] = "not-accepted"
    elif bad == "missing":
        del data["recipient_personnel_id"]
    elif bad == "foreign":
        data["recipient_personnel_id"] = str(uuid4())
    else:
        data["transfer_request_id"] = 123
    assert tablet.post(API, json.dumps(data), content_type="application/json").status_code in (
        400,
        403,
    )
    assert mail.send.call_count == 0 and ReportTransfer.objects.count() == 0


@pytest.mark.parametrize(
    "invalid",
    ["inactive", "unverified", "ineligible", "missing_email", "expired", "lease", "wrong_station"],
)
def test_issue_rechecks_authorization(submitted, mail, invalid):
    installation, person, _, row, _ = submitted
    if invalid == "inactive":
        person.active = False
        person.lifecycle_status = "DEPARTED"
        person.departed_at = timezone.now()
    elif invalid == "unverified":
        person.email_verified_at = None
    elif invalid == "ineligible":
        person.incident_commander_eligible = False
    elif invalid == "missing_email":
        person.incident_commander_email = ""
    elif invalid == "expired":
        row.expires_at = timezone.now()
        row.save()
    elif invalid == "lease":
        installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
        installation.save()
    else:
        from apps.organizations.models import Station

        row.station = Station.objects.create(
            department=row.department, name="Other", short_code="O"
        )
        row.save()
    person.save()
    response, _ = issue(submitted, mail)
    assert response.status_code in (403, 404, 409, 410) and mail.send.call_count == 0


@pytest.mark.parametrize(
    "failure,state",
    [(TimeoutError("sensitive-provider-data"), "UNKNOWN"), (MessageRejectedError(), "FAILED")],
)
def test_mail_failure_no_retries(submitted, mail, failure, state, caplog):
    mail.send.side_effect = failure
    request_id = uuid4()
    response, _ = issue(submitted, mail, request_id=request_id)
    assert response.json()["status"] == state
    assert issue(submitted, mail, request_id=request_id)[0].json() == response.json()
    assert mail.send.call_count == 1 and "sensitive-provider-data" not in caplog.text
    mail.send.side_effect = None
    replacement, _ = issue(submitted, mail)
    assert replacement.json()["status"] == "SENT"


def test_rate_limit_allows_replacement_but_bounds_mail(submitted, mail):
    for _ in range(3):
        assert issue(submitted, mail)[0].status_code == 201
    assert issue(submitted, mail)[0].status_code == 429
    assert mail.send.call_count == 3
    active = ReportTransfer.objects.get(revoked_at__isnull=True)
    assert submitted[2].post(f"{API}/{active.pk}/revoke").status_code == 200


def test_access_errors_never_log_bearer_paths(submitted, mail, caplog):
    caplog.set_level(logging.DEBUG)
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    token = path.split("/")[-2]
    client = Client(enforce_csrf_checks=True)
    client.post(path + "redeem/", secure=True)
    client.get(path + "revoke/", secure=True)
    assert token not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_parallel_redemption_only_once(submitted, mail):
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return Client().post(path + "redeem/").status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == [302, 409]
    assert ReportPortalSession.objects.count() == 1
    assert AuditEvent.objects.filter(action="report_transfer.login_succeeded").count() == 1
    assert AuditEvent.objects.filter(action="report_transfer.login_failed").count() == 1


@pytest.mark.django_db(transaction=True)
def test_parallel_issue_replay_only_one_mail(submitted, mail):
    installation, _, _, row, _ = submitted
    request_id = str(uuid4())
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return transfers.request_transfer(
                installation=installation,
                data={
                    "transfer_request_id": request_id,
                    "recipient_personnel_id": str(row.recipient_id),
                },
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    assert mail.send.call_count == 1 and ReportTransfer.objects.count() == 1
    assert AuditEvent.objects.filter(action="report_transfer.created").count() == 1
    assert AuditEvent.objects.filter(action="report_transfer.link_issued").count() == 1


def add_submission(submitted, report_key, *, recipient=None, full_document=False):  # noqa: F811
    installation, person, client, _, _ = submitted
    header = {
        "typ": "firedash-report+jwe",
        "alg": "RSA-OAEP-256",
        "enc": "A256GCM",
        "kid": "test-1",
        "package_version": 1,
        "schema_version": "transport-json-v1",
        "submission_id": str(uuid4()),
        "report_id": str(uuid4()),
        "recipient_personnel_id": str((recipient or person).pk),
        "installation_id": str(installation.pk),
        "department_id": str(installation.tablet.department_id),
        "station_id": str(submitted[3].station_id),
    }
    payload = package(header)
    if full_document:
        from apps.report_transfer.contract import decode

        document = json.loads(decode(payload["report"]["data"]))
        document["title"] = 'Beliebiger Typ & "Zitat"'
        document["sections"][0]["blocks"].extend(
            [
                {
                    "kind": "fields",
                    "key": "f",
                    "fields": [
                        {
                            "key": "bool",
                            "label": "Bestätigt",
                            "value": {"type": "boolean", "value": True},
                        }
                    ],
                },
                {
                    "kind": "table",
                    "key": "t",
                    "columns": [{"key": "a", "label": "Spalte"}],
                    "rows": [[{"type": "text", "value": "&lt;script&gt;alert(1)&lt;/script&gt;"}]],
                },
                {
                    "kind": "attachment_refs",
                    "key": "a",
                    "attachment_ids": [payload["attachments"][0]["id"]],
                },
            ]
        )
        payload["report"].update(content(json.dumps(document).encode()))
    assert post(client, encrypt(report_key, header, payload)).status_code == 201
    return ReportSubmission.objects.get(submission_id=header["submission_id"])


def test_multiple_reports_all_blocks_safe_html_and_conflicting_replay(submitted, mail, report_key):  # noqa: F811
    second = add_submission(submitted, report_key, full_document=True)
    request_id = uuid4()
    response, url = issue(submitted, mail, request_id=request_id)
    assert response.status_code == 201
    assert issue(submitted, mail, request_id=request_id)[0].status_code == 200
    assert mail.send.call_count == 1
    assert issue(submitted, mail, request_id=request_id, recipient_id=uuid4())[0].status_code == 409
    browser, _, _ = browser_login(url)
    assert browser.get("/reports/").content.decode().count("Bericht öffnen") == 2
    html = browser.get(f"/reports/{second.report_id}/").content.decode()
    assert "Bestätigt" in html and "Ja" in html and "Spalte" in html
    assert "&amp;lt;script&amp;gt;" in html and "<script>" not in html
    assert "Beliebiger Typ &amp; &quot;Zitat&quot;" in html


def test_distinct_recipients_isolated_and_mixed_request_rejected(submitted, mail, report_key):  # noqa: F811
    from apps.personnel.models import Person

    other = Person.objects.create(
        department=submitted[3].department,
        display_name="Other commander",
        incident_commander_eligible=True,
        incident_commander_email="other@example.test",
        email_verified_at=timezone.now(),
    )
    second = add_submission(submitted, report_key, recipient=other)
    _, first_url = issue(submitted, mail)
    first_browser, _, _ = browser_login(first_url)
    first_transfer = ReportTransfer.objects.get()
    assert list(first_transfer.submissions.values_list("pk", flat=True)) == [submitted[3].pk]
    second_response, second_url = issue(submitted, mail, recipient_id=other.pk)
    assert second_response.status_code == 201
    second_browser, _, _ = browser_login(second_url)
    assert first_browser.get("/reports/").status_code == 200
    assert first_browser.get(f"/reports/{second.report_id}/pdf/").status_code == 404
    assert second_browser.get(f"/reports/{submitted[3].report_id}/").status_code == 404


def test_other_installation_cannot_issue_or_revoke(submitted, mail):
    import hashlib
    import hmac

    from django.conf import settings

    from apps.assignments.models import TabletVehicleAssignment
    from apps.tablets.models import Tablet

    response, url = issue(submitted, mail)
    installation, _, _, row, _ = submitted
    tablet = Tablet.objects.create(
        department=installation.tablet.department, display_name="Other tablet", status="ACTIVE"
    )
    assignment = TabletVehicleAssignment.objects.get(tablet=installation.tablet)
    TabletVehicleAssignment.objects.create(
        tablet=tablet,
        vehicle=assignment.vehicle,
        valid_from=timezone.now() - timedelta(minutes=1),
        created_by=row.department.created_by,
    )
    installation.pk = None
    installation.tablet = tablet
    installation.installation_uuid = uuid4()
    installation.credential_hash = hmac.new(
        settings.SECRET_KEY.encode(), b"other-credential", hashlib.sha256
    ).hexdigest()
    installation.save(force_insert=True)
    client = Client(HTTP_AUTHORIZATION="Bearer other-credential")
    request = {"transfer_request_id": str(uuid4()), "recipient_personnel_id": str(row.recipient_id)}
    assert client.post(API, json.dumps(request), content_type="application/json").status_code == 409
    assert client.post(f"{API}/{response.json()['transfer_id']}/revoke").status_code == 404
    assert Client().post(API, json.dumps(request), content_type="application/json").status_code in (
        401,
        403,
    )
    assert Client(HTTP_AUTHORIZATION="Bearer invalid").post(
        API, json.dumps(request), content_type="application/json"
    ).status_code in (401, 403)
    assert mail.send.call_count == 1
    assert ReportTransfer.objects.get().revoked_at is None


def test_policy_revocation_and_mail_provider_unavailable(submitted, mail, monkeypatch):
    from apps.outbound_mail.models import DepartmentRecipientPolicy

    policy = DepartmentRecipientPolicy.objects.create(
        department=submitted[3].department, restriction_enabled=True
    )
    assert issue(submitted, mail)[0].status_code == 403
    policy.delete()
    monkeypatch.setattr(
        transfers, "resolve_department_mail_provider", Mock(return_value=Mock(is_usable=False))
    )
    response, _ = issue(submitted, mail)
    assert response.json()["status"] == "FAILED"
    assert ReportTransfer.objects.get().mail_code == "provider_unavailable"
    assert mail.send.call_count == 0


def test_logout_replay_cookie_and_confirmed_portal_revoke(submitted, mail):
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    cookie = browser.cookies["sessionid"].value
    assert browser.post("/reports/revoke/").status_code == 200
    assert ReportTransfer.objects.get().revoked_at is None
    assert browser.post("/reports/revoke/", {"confirm": "yes"}).status_code == 302
    replay = Client()
    replay.cookies["sessionid"] = cookie
    assert replay.get("/reports/").status_code == 403
    assert issue(submitted, mail)[0].status_code == 201


def test_session_rotates_and_cannot_carry_admin_identity(submitted, mail):
    _, url = issue(submitted, mail)
    browser = Client()
    browser.force_login(submitted[3].department.created_by)
    old_key = browser.cookies["sessionid"].value
    path = url.removeprefix("https://firedash.test")
    assert browser.post(path + "redeem/").status_code == 302
    assert browser.cookies["sessionid"].value != old_key
    assert "_auth_user_id" not in browser.session
    assert browser.get("/reports/").status_code == 200
    assert browser.post("/reports/logout/").status_code == 302
    assert browser.get("/reports/").status_code == 403


def test_csrf_revoke_requires_valid_origin_and_token(submitted, mail):
    _, url = issue(submitted, mail)
    browser, path, response = browser_login(url, csrf=True)
    assert response.status_code == 302
    token = browser.cookies["csrftoken"].value
    assert (
        browser.post(
            path + "revoke/", {"confirm": "yes"}, secure=True, HTTP_ORIGIN="https://testserver"
        ).status_code
        == 403
    )
    assert (
        browser.post(
            path + "revoke/",
            {"confirm": "yes", "csrfmiddlewaretoken": token},
            secure=True,
            HTTP_ORIGIN="https://evil.test",
        ).status_code
        == 403
    )
    assert ReportTransfer.objects.get().revoked_at is None
    assert (
        browser.post(
            path + "revoke/",
            {"confirm": "yes", "csrfmiddlewaretoken": token},
            secure=True,
            HTTP_ORIGIN="https://testserver",
        ).status_code
        == 302
    )


def test_revoke_interrupts_an_inflight_download(submitted, mail):
    from django.test import RequestFactory

    from apps.report_transfer.portal import download_response

    response, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    request = RequestFactory().get("/reports/")
    request.session = browser.session
    row = submitted[3]
    stream_response = download_response(request, row, b"x" * 600_000, "application/pdf", "pdf")
    stream = iter(stream_response.streaming_content)
    assert len(next(stream)) == 256 * 1024
    submitted[2].post(f"{API}/{response.json()['transfer_id']}/revoke")
    with pytest.raises(TransferError):
        next(stream)
    row.refresh_from_db()
    assert row.first_download_at is None


def test_unexpected_token_failure_is_sanitized(submitted, mail, monkeypatch, caplog):
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    token = path.split("/")[-2]
    monkeypatch.setattr(
        transfers,
        "token_transfer",
        Mock(side_effect=RuntimeError("private-report-marker " + token)),
    )
    response = Client().get(path)
    assert response.status_code == 503
    assert token not in caplog.text and "private-report-marker" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_old_revoke_waiting_for_issue_cannot_revoke_new_generation(submitted, mail, monkeypatch):
    from threading import Event

    from django.db import transaction

    _, url = issue(submitted, mail)
    browser_login(url)
    token = url.split("/")[-2]
    started = Event()
    real_lock = transfers.lock_installation

    def waiting_lock(pk):
        started.set()
        return real_lock(pk)

    def revoke_worker():
        close_old_connections()
        try:
            with pytest.raises(TransferError):
                transfers.revoke_token(token)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            real_lock(submitted[0].pk)
            monkeypatch.setattr(transfers, "lock_installation", waiting_lock)
            future = pool.submit(revoke_worker)
            assert started.wait(timeout=10)
            new, _ = issue(submitted, mail)
            assert new.status_code == 201
        future.result(timeout=10)
    assert ReportTransfer.objects.get(pk=new.json()["transfer_id"]).revoked_at is None


@pytest.mark.django_db(transaction=True)
def test_completion_database_failure_journal_enforces_deadline_and_cleanup_retries(
    submitted, mail, monkeypatch, caplog
):
    from django.db import OperationalError

    from apps.report_transfer import portal, storage
    from apps.report_transfer.services import cleanup, effective_expiry

    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row = submitted[3]
    real_record = portal.record_successful_download
    monkeypatch.setattr(
        portal,
        "record_successful_download",
        Mock(side_effect=OperationalError("secret-driver-diagnostic")),
    )
    response = browser.get(f"/reports/{row.report_id}/pdf/")
    assert b"".join(response.streaming_content).startswith(b"%PDF-")
    row.refresh_from_db()
    assert row.first_download_at is None
    receipts = list(storage.root().glob("*.receipt"))
    assert len(receipts) == 1 and "secret-driver-diagnostic" not in caplog.text
    _, completed_at = storage.read_download_receipt(receipts[0])
    assert effective_expiry(row) == completed_at + timedelta(hours=48)
    monkeypatch.setattr(portal, "record_successful_download", real_record)
    assert cleanup()["failed"] == 0
    row.refresh_from_db()
    assert row.first_download_at == completed_at
    assert row.expires_at == completed_at + timedelta(hours=48)
    assert not list(storage.root().glob("*.receipt"))
    assert cleanup()["failed"] == 0


def test_late_receipt_recovery_and_interrupted_receipt_file_cleanup(submitted):
    import os

    from apps.report_transfer import storage
    from apps.report_transfer.services import cleanup

    row = submitted[3]
    now = timezone.now()
    ReportSubmission.objects.filter(pk=row.pk).update(
        created_at=now - timedelta(days=4), expires_at=now + timedelta(days=3)
    )
    storage.write_download_receipt(row.pk, now - timedelta(days=3))
    temporary = storage.root() / f"{row.pk}-{uuid4()}.receipt-tmp"
    temporary.write_bytes(b"interrupted")
    old = (now - timedelta(hours=2)).timestamp()
    os.utime(temporary, (old, old))
    result = cleanup()
    row.refresh_from_db()
    assert result == {"expired": 1, "failed": 0, "orphans": 1}
    assert row.state == "EXPIRED" and row.first_download_at == now - timedelta(days=3)
    assert not temporary.exists() and not list(storage.root().glob("*.receipt"))


def test_used_token_revoke_does_not_depend_on_remaining_recipient_eligibility(submitted, mail):
    _, url = issue(submitted, mail)
    browser, path, _ = browser_login(url)
    person = submitted[1]
    person.email_verified_at = None
    person.save()
    assert "bereits verwendet" in Client().get(path).content.decode()
    assert Client().post(path + "revoke/", {"confirm": "yes"}).status_code == 302
    assert ReportTransfer.objects.get().revoked_at is not None
    assert browser.get("/reports/").status_code == 403


def test_prefetch_never_counts_as_explicit_download(submitted, mail):
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row = submitted[3]
    assert (
        browser.get(f"/reports/{row.report_id}/pdf/", HTTP_SEC_PURPOSE="prefetch").status_code
        == 400
    )
    row.refresh_from_db()
    assert row.first_download_at is None


@pytest.mark.django_db(transaction=True)
def test_asgi_stream_records_only_after_full_iteration(submitted, mail):
    import asyncio

    from asgiref.sync import sync_to_async
    from django.db import connections
    from django.test import AsyncClient

    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row = submitted[3]

    async def consume():
        client = AsyncClient()
        client.cookies = browser.cookies
        try:
            response = await client.get(f"/reports/{row.report_id}/pdf/", secure=True)
            assert response.status_code == 200 and response.is_async
            return b"".join([chunk async for chunk in response.streaming_content])
        finally:
            # AsyncClient's ORM executor outlives the event loop. Explicitly
            # release its test connection so PostgreSQL teardown stays isolated.
            await sync_to_async(connections.close_all)()

    assert asyncio.run(consume()).startswith(b"%PDF-")
    row.refresh_from_db()
    assert row.first_download_at is not None


def test_partial_expiry_does_not_hide_remaining_selected_reports(submitted, mail, report_key):  # noqa: F811
    second = add_submission(submitted, report_key)
    _, url = issue(submitted, mail)
    ReportSubmission.objects.filter(pk=submitted[3].pk).update(expires_at=timezone.now())
    browser, _, response = browser_login(url)
    assert response.status_code == 302
    assert browser.get("/reports/").content.decode().count("Bericht öffnen") == 1
    assert browser.get(f"/reports/{second.report_id}/").status_code == 200
    assert browser.get(f"/reports/{submitted[3].report_id}/").status_code == 404
