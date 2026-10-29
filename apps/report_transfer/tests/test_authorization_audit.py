"""Issued grants retain historical scope; audit only observed server events."""

# Imported pytest fixtures intentionally share names with test arguments.
# ruff: noqa: F811

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import close_old_connections
from django.test import Client
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.audit.models import AuditEvent
from apps.organizations.models import Station, Vehicle
from apps.outbound_mail.tests.test_report_delivery_requests import request_context  # noqa: F401
from apps.report_transfer import storage
from apps.report_transfer.models import ReportTransfer
from apps.report_transfer.services import cleanup
from apps.report_transfer.tests.test_portal import (  # noqa: F401
    browser_login,
    issue,
    mail,
    submitted,
)
from apps.report_transfer.tests.test_submissions import (  # noqa: F401
    context,
    encrypt,
    package,
    post,
    report_key,
)

pytestmark = pytest.mark.django_db


def events(suffix):
    return AuditEvent.objects.filter(action="report_transfer." + suffix)


def move_tablet(installation):
    now = timezone.now()
    TabletVehicleAssignment.objects.filter(tablet=installation.tablet).update(ended_at=now)
    department = installation.tablet.department
    station = Station.objects.create(department=department, name="New station", short_code="N")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="New car")
    TabletVehicleAssignment.objects.create(
        tablet=installation.tablet,
        vehicle=vehicle,
        valid_from=now,
        created_by=department.created_by,
    )
    return station


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["expired_lease", "stale", "assignment"])
@pytest.mark.parametrize("already_logged_in", [False, True])
def test_issued_portal_access_survives_operational_changes(
    submitted, mail, change, already_logged_in
):
    installation, _, _, row, payload = submitted
    _, url = issue(submitted, mail)
    if already_logged_in:
        browser, _, response = browser_login(url)
        assert response.status_code == 302
    if change == "assignment":
        move_tablet(installation)
    else:
        installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
        if change == "stale":
            installation.status = "STALE"
            installation.stale_at = timezone.now()
        installation.save()
    if not already_logged_in:
        browser, _, response = browser_login(url)
        assert response.status_code == 302
    root = f"/reports/{row.report_id}/"
    photo = payload["attachments"][0]["id"]
    for path in ("/reports/", root, root + f"photos/{photo}/preview/"):
        assert browser.get(path).status_code == 200
    for path in (root + "pdf/", root + f"photos/{photo}/"):
        response = browser.get(path)
        assert response.status_code == 200
        assert b"".join(response.streaming_content)
        response.close()
    # Only issuance continues to require today's operational authorization/scope.
    denied, _ = issue(submitted, mail)
    assert denied.status_code == (404 if change == "assignment" else 403)
    assert mail.send.call_count == 1


def test_historical_scope_stays_bound_to_original_report(submitted, mail, report_key):  # noqa: F811
    installation, _, tablet, row, _ = submitted
    original_station = row.station_id
    _, url = issue(submitted, mail)
    new_station = move_tablet(installation)
    header = {
        "typ": "firedash-report+jwe",
        "alg": "RSA-OAEP-256",
        "enc": "A256GCM",
        "kid": "test-1",
        "package_version": 1,
        "schema_version": "transport-json-v1",
        "submission_id": str(uuid4()),
        "report_id": str(uuid4()),
        "recipient_personnel_id": str(row.recipient_id),
        "installation_id": str(installation.pk),
        "department_id": str(row.department_id),
        "station_id": str(new_station.pk),
    }
    envelope = encrypt(report_key, header, package(header))
    assert post(tablet, envelope).status_code == 201
    browser, _, response = browser_login(url)
    assert response.status_code == 302
    assert browser.get(f"/reports/{row.report_id}/").status_code == 200
    assert browser.get(f"/reports/{header['report_id']}/").status_code == 404
    row.refresh_from_db()
    assert row.station_id == original_station
    # An altered historical reference is not accepted as valid report scope.
    row.station = new_station
    row.save()
    assert browser.get(f"/reports/{row.report_id}/").status_code == 409
    assert browser.get(f"/reports/{row.report_id}/pdf/").status_code == 409


@pytest.mark.parametrize("already_logged_in", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "REVOKED",
        "REPLACED",
        "LOST",
        "RETIRED",
        "INACTIVE",
        "disabled",
        "department",
        "recipient",
        "transfer",
    ],
)
def test_explicit_security_revocations_still_block_access(
    submitted, mail, change, already_logged_in
):
    installation, person, _, row, payload = submitted
    _, url = issue(submitted, mail)
    if already_logged_in:
        browser, _, _ = browser_login(url)
    if change in ("REVOKED", "REPLACED"):
        installation.status = change
        installation.save()
    elif change in ("LOST", "RETIRED", "INACTIVE", "disabled"):
        tablet = installation.tablet
        if change == "disabled":
            tablet.active = False
        else:
            tablet.status = change
        tablet.save()
    elif change == "department":
        row.department.status = "DEACTIVATED"
        row.department.save()
    elif change == "recipient":
        person.incident_commander_eligible = False
        person.save()
    else:
        ReportTransfer.objects.update(revoked_at=timezone.now())
    if not already_logged_in:
        path = url.removeprefix("https://firedash.test")
        assert Client().post(path + "redeem/").status_code == 403
        assert events("login_succeeded").count() == 0
    else:
        root = f"/reports/{row.report_id}/"
        photo = payload["attachments"][0]["id"]
        for path in (
            "/reports/",
            root,
            root + "pdf/",
            root + f"photos/{photo}/",
            root + f"photos/{photo}/preview/",
        ):
            assert browser.get(path).status_code in (403, 404)
        assert events("pdf_generated").count() == 0
    assert events("download_completed").count() == 0


@pytest.mark.django_db(transaction=True)
def test_observed_lifecycle_audit_and_safe_metadata(submitted, mail):
    installation, person, tablet, row, envelope = submitted
    accepted = events("submission_accepted").get()
    assert accepted.actor_installation_uuid == installation.installation_uuid
    assert accepted.target_uuid == row.submission_id
    request_id = uuid4()
    first, url = issue(submitted, mail, request_id=request_id)
    assert first.status_code == 201
    assert issue(submitted, mail, request_id=request_id)[0].status_code == 200
    assert events("created").count() == events("link_issued").count() == 1
    assert events("link_issued").get().metadata == {"result": "SENT", "code": "delivered"}
    assert events("created").get().actor_installation_uuid == installation.installation_uuid
    browser, path, response = browser_login(url)
    assert response.status_code == 302
    assert events("login_succeeded").count() == 1
    assert Client().post(path + "redeem/").status_code == 409
    assert events("login_failed").get().metadata == {"code": "access_used"}
    root = f"/reports/{row.report_id}/"
    photo = envelope["attachments"][0]["id"]
    assert browser.get(root).status_code == 200
    assert browser.get(root + f"photos/{photo}/preview/").status_code == 200
    assert events("download_completed").count() == 0
    for path, file_type in ((root + "pdf/", "pdf"), (root + f"photos/{photo}/", "jpeg")):
        response = browser.get(path)
        assert events("download_completed").filter(metadata__file_type=file_type).count() == 0
        assert b"".join(response.streaming_content)
        response.close()
        event = events("download_completed").get(metadata__file_type=file_type)
        assert event.metadata["transfer_id"] == first.json()["transfer_id"]
        assert event.actor_user_id is None and event.actor_installation_uuid is None
    assert events("pdf_generated").count() == 1
    assert (
        Client()
        .post(url.removeprefix("https://firedash.test") + "revoke/", {"confirm": "yes"})
        .status_code
        == 302
    )
    assert events("revoked").get().metadata["source"] == "consumed_token"
    assert browser.get(root + "pdf/").status_code == 403
    assert events("pdf_generated").count() == 1
    assert issue(submitted, mail)[0].status_code == 201
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save()
    assert cleanup()["expired"] == 1
    assert cleanup()["expired"] == 0
    assert events("content_purged").count() == 1
    # Explicit allowlist prevents content/identities/credentials entering metadata.
    allowed = {
        "report_id",
        "installation_id",
        "accepted_at",
        "issued_at",
        "result",
        "code",
        "consumed_at",
        "source",
        "revoked_at",
        "file_type",
        "transfer_id",
        "download_id",
        "completed_at",
    }
    for event in AuditEvent.objects.filter(action__startswith="report_transfer."):
        assert set(event.metadata) <= allowed
        assert event.actor_user_id is None
        assert not event.source_ip and not event.user_agent
        serialized = json.dumps(event.metadata)
        for secret in (
            person.incident_commander_email,
            person.display_name,
            url.split("/")[-2],
            ReportTransfer.objects.get(pk=first.json()["transfer_id"]).token_digest,
            row.payload_digest,
            envelope["report"]["data"],
            envelope["attachments"][0]["data"],
            browser.session.session_key,
        ):
            assert secret not in serialized


@pytest.mark.parametrize("source", ["tablet", "portal", "replacement"])
def test_revocation_audit_source_and_idempotency(submitted, mail, source):
    _, url = issue(submitted, mail)
    transfer = ReportTransfer.objects.get()
    if source == "tablet":
        for _ in range(2):
            response = submitted[2].post(f"/api/v1/tablet/report-transfers/{transfer.pk}/revoke")
            assert response.status_code == 200
    elif source == "portal":
        browser, _, _ = browser_login(url)
        assert browser.post("/reports/revoke/", {"confirm": "yes"}).status_code == 302
    else:
        assert issue(submitted, mail)[0].status_code == 201
    event = events("revoked").get()
    assert event.target_uuid == transfer.pk and event.metadata["source"] == source
    assert (event.actor_installation_uuid is not None) == (source != "portal")


def test_failed_login_audit_is_bounded_and_unknown_tokens_not_retained(submitted, mail):
    _, url = issue(submitted, mail)
    path = url.removeprefix("https://firedash.test")
    ReportTransfer.objects.update(token_expires_at=timezone.now())
    client = Client()
    for _ in range(5):
        assert client.post(path + "redeem/").status_code == 403
        assert client.post("/reports/access/" + "a" * 43 + "/redeem/").status_code == 403
    assert events("login_failed").filter(target_uuid__isnull=False).count() == 1
    unknown = events("login_failed").get(target_uuid__isnull=True)
    assert unknown.metadata == {"code": "unidentified_token"}
    assert unknown.actor_user_id is None and not unknown.user_agent and not unknown.source_ip
    assert events("login_succeeded").count() == 0


def test_failed_mail_and_replay_never_audit_success(submitted, mail):
    mail.send.side_effect = RuntimeError("untrusted provider data")
    request_id = uuid4()
    response, _ = issue(submitted, mail, request_id=request_id)
    assert response.json()["status"] == "UNKNOWN"
    issue(submitted, mail, request_id=request_id)
    event = events("link_issued").get()
    assert event.metadata == {"result": "UNKNOWN", "code": "delivery_indeterminate"}
    assert events("login_succeeded").count() == 0


@pytest.mark.django_db(transaction=True)
def test_unknown_token_audit_is_bounded_across_parallel_requests(monkeypatch):
    barrier = Barrier(3)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return Client().post("/reports/access/" + "a" * 43 + "/redeem/").status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: worker(), range(3))) == [403, 403, 403]
    assert events("login_failed").count() == 1
    future = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("apps.report_transfer.transfers.timezone.now", lambda: future)
    assert Client().post("/reports/access/invalid/redeem/").status_code == 403
    assert events("login_failed").count() == 2


def test_submission_audit_only_on_first_acceptance_and_not_on_failure(submitted):
    row, tablet = submitted[3], submitted[2]
    envelope = json.loads(storage.payload_path(row.payload_key).read_bytes())
    assert post(tablet, envelope).status_code == 200
    assert post(tablet, {}).status_code == 400
    assert events("submission_accepted").count() == 1


def test_submission_audit_rolls_back_with_failed_acceptance(context, report_key, monkeypatch):
    from apps.report_transfer import services

    _, _, tablet, header = context
    record = services.record_event

    def interrupted_audit(**kwargs):
        record(**kwargs)
        raise OSError("interrupted transaction")

    monkeypatch.setattr(services, "record_event", interrupted_audit)
    assert post(tablet, encrypt(report_key, header, package(header))).status_code == 503
    assert events("submission_accepted").count() == 0
    assert not list(storage.root().glob("*.jwe"))


@pytest.mark.django_db(transaction=True)
def test_interrupted_and_failed_actions_have_no_success_audit(submitted, mail, monkeypatch):
    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    row = submitted[3]
    response = browser.get(f"/reports/{row.report_id}/pdf/")
    assert next(iter(response.streaming_content)).startswith(b"%PDF")
    response.close()
    assert events("pdf_generated").count() == 1
    assert events("download_completed").count() == 0
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save()
    real_remove = storage.remove_payload
    monkeypatch.setattr(storage, "remove_payload", Mock(side_effect=OSError("private failure")))
    assert cleanup()["failed"] == 1
    assert events("content_purged").count() == 0
    monkeypatch.setattr(storage, "remove_payload", real_remove)
    assert cleanup()["expired"] == 1
    assert events("content_purged").count() == 1


@pytest.mark.django_db(transaction=True)
def test_download_audit_recovery_is_exactly_once(submitted, mail, monkeypatch):
    from apps.report_transfer import portal

    _, url = issue(submitted, mail)
    browser, _, _ = browser_login(url)
    real_record = portal.record_successful_download
    monkeypatch.setattr(portal, "record_successful_download", Mock(side_effect=OSError()))
    response = browser.get(f"/reports/{submitted[3].report_id}/pdf/")
    assert b"".join(response.streaming_content)
    response.close()
    receipt = next(storage.root().glob("*.receipt"))
    original = receipt.read_bytes()
    assert events("download_completed").count() == 0
    monkeypatch.setattr(portal, "record_successful_download", real_record)
    assert cleanup()["failed"] == 0
    assert events("download_completed").get().metadata["file_type"] == "pdf"
    receipt.write_bytes(original)  # committed completion, interrupted receipt removal
    assert cleanup()["failed"] == 0
    assert events("download_completed").count() == 1
