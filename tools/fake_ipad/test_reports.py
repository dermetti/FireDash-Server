"""Offline report tooling and independent wire-contract integration tests."""

# Imported fixtures are injected by pytest.
# ruff: noqa: F811

import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import close_old_connections
from django.utils import timezone
from PIL import Image

from apps.personnel.models import Person
from apps.report_transfer import services, transfers
from apps.report_transfer.contract import configuration, decrypt_and_validate, inspect_envelope
from apps.report_transfer.models import ReportSubmission, ReportTransfer
from apps.report_transfer.tests.test_authorization_audit import move_tablet
from apps.report_transfer.tests.test_portal import (  # noqa: F401
    API,
    add_submission,
    browser_login,
    context,
    issue,
    mail,
    report_key,
    request_context,
    submitted,
)
from tools.fake_ipad.cli import main
from tools.fake_ipad.client import FakeIPadClient
from tools.fake_ipad.errors import ClientError
from tools.fake_ipad.output import Output
from tools.fake_ipad.reports import ReportTest, canonical, envelope, synthetic
from tools.fake_ipad.state import DeviceState
from tools.fake_ipad.transport import ApiClient, HttpResponse


@pytest.fixture
def runner(context, tmp_path):
    installation, _, tablet, _ = context
    state = DeviceState(tmp_path / "ipad")
    state.state = {
        "installation_id": str(installation.pk),
        "tablet_id": str(installation.tablet_id),
        "credential": tablet.defaults["HTTP_AUTHORIZATION"].removeprefix("Bearer "),
    }

    class LocalTransport:
        server_url = "https://firedash.test"
        expect = ApiClient.expect

        def call(self, method, path, *, json_body=None, bearer=None, headers=None):
            assert bearer == state.credential
            response = tablet.generic(
                method,
                path,
                data=canonical(json_body) if json_body is not None else b"",
                content_type="application/json",
            )
            return HttpResponse(
                response.status_code, dict(response.items()), response.content, path
            )

    return ReportTest(FakeIPadClient(state, LocalTransport(), out=Output(stream=io.StringIO())))


@pytest.mark.django_db
def test_five_independent_jwes_real_acceptance_replay_conflict_and_private_queue(
    context, runner, tmp_path
):
    installation, person, _, _ = context
    # Save an unrelated dataset artifact/identity to prove report commands leave it alone.
    existing = runner.client.state.state_dir / "dataset-marker"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"unchanged")
    original_state = dict(runner.client.state.state)
    result = runner.send(str(person.pk), storm=True)
    assert len(result["reports"]) == ReportSubmission.objects.count() == 5
    report_ids, submission_ids, finalized = set(), set(), []
    for index, item in enumerate(result["reports"]):
        assert item["http_status"] == 201 and item["state"] == "READY"
        assert item["attachment_count"] == 3
        record = runner.load(item["submission_id"])
        jwe = record["envelope"]
        assert set(jwe) == {"protected", "encrypted_key", "iv", "ciphertext", "tag"}
        document, attachments, _ = decrypt_and_validate(jwe, inspect_envelope(jwe))
        assert document["report_type"] == "incident"
        assert f"TEST-{index + 1:03d}" in document["title"]
        assert len(attachments) == 3
        assert {b["kind"] for b in document["sections"][0]["blocks"]} == {
            "text",
            "fields",
            "table",
            "attachment_refs",
        }
        report_ids.add(document["report_id"])
        submission_ids.add(item["submission_id"])
        finalized.append(document["finalization"]["finalized_at"])
        assert runner.retry(item["submission_id"])["http_status"] == 200
        _, other_document, photos = synthetic(index)
        conflict = envelope(
            runner.client.get_configuration(),
            str(person.pk),
            other_document,
            photos,
            item["submission_id"],
        )
        with pytest.raises(ClientError, match="HTTP 409 code=idempotency_conflict"):
            runner.call("POST", "/report-submissions", conflict, (201,))
    assert len(report_ids) == len(submission_ids) == 5
    assert finalized == sorted(finalized)
    assert existing.read_bytes() == b"unchanged"
    assert runner.client.state.state == original_state
    for path in runner.root.glob("*.json"):
        raw = path.read_text("utf-8")
        assert all(
            secret not in raw
            for secret in (
                "SIMULATED",
                "Hauptstrasse",
                "BEGIN",
                installation.credential_hash,
                runner.client.state.credential,
                "display_name",
                "image/jpeg",
            )
        )


@pytest.mark.django_db(transaction=True)
def test_fixed_five_snapshot_later_upload_replay_consumed_token_and_replacement(
    context, runner, mail
):
    person = context[1]
    first_batch = runner.send(str(person.pk), storm=True)
    outcome = runner.link(str(person.pk))
    transfer = ReportTransfer.objects.get(pk=outcome["transfer_id"])
    original_ids = set(transfer.submissions.values_list("submission_id", flat=True))
    assert len(original_ids) == 5
    url = mail.send.call_args.args[0].body.split()[-1]
    browser, old_path, response = browser_login(url)
    assert response.status_code == 302
    cards = browser.get("/reports/")
    assert cards.status_code == 200
    html = cards.content.decode()
    positions = [html.index(f"TEST-{i:03d}") for i in range(1, 6)]
    assert positions == sorted(positions)
    assert html.count("3 Fotos") == 5 and "Client-Angabe" in html
    assert "UTC ?" not in html and "UTC; 3 Fotos" in html
    assert "Hauptstrasse" in html and "Eichenweg" in html
    pdf_names, jpeg_names = set(), set()
    for row in transfer.submissions.all():
        document, _, _ = decrypt_and_validate(
            runner.load(str(row.submission_id))["envelope"],
            inspect_envelope(runner.load(str(row.submission_id))["envelope"]),
        )
        assert browser.get(f"/reports/{row.report_id}/").status_code == 200
        pdf = browser.get(f"/reports/{row.report_id}/pdf/")
        pdf_name = pdf["Content-Disposition"]
        incident = document["header"][0]["value"]["value"].lower()
        assert incident in pdf_name and str(row.report_id) in pdf_name
        pdf_names.add(pdf_name)
        assert b"".join(pdf.streaming_content).startswith(b"%PDF-")
        pdf.close()
        for number, attachment in enumerate(document["attachments"], start=1):
            photo_url = f"/reports/{row.report_id}/photos/{attachment['id']}/"
            assert browser.get(photo_url + "preview/").status_code == 200
            jpeg = browser.get(photo_url)
            name = jpeg["Content-Disposition"]
            assert incident in name and f"-foto-{number:02d}-test-foto-{number}-" in name
            jpeg_names.add(name)
            assert b"".join(jpeg.streaming_content).startswith(b"\xff\xd8")
            jpeg.close()
        repeated_pdf = browser.get(f"/reports/{row.report_id}/pdf/")
        assert repeated_pdf["Content-Disposition"] == pdf_name
        repeated_pdf.close()
        repeated_jpeg = browser.get(photo_url)
        assert repeated_jpeg["Content-Disposition"] == name
        repeated_jpeg.close()
    assert len(pdf_names) == 5 and len(jpeg_names) == 15
    later = runner.send(str(person.pk))["reports"][0]
    request_id = str(transfer.transfer_request_id)
    assert runner.link(str(person.pk), request_id=request_id) == outcome
    assert mail.send.call_count == 1
    assert set(transfer.submissions.values_list("submission_id", flat=True)) == original_ids
    assert browser.get(f"/reports/{later['report_id']}/").status_code == 404
    assert browser.post(old_path + "redeem/").status_code == 409
    newer = runner.link(str(person.pk))
    assert newer != outcome and mail.send.call_count == 2
    assert ReportTransfer.objects.get(pk=newer["transfer_id"]).submissions.count() == 6
    assert browser.get("/reports/").status_code == 403
    assert browser.post(old_path + "revoke/", {"confirm": "yes"}).status_code == 403
    assert runner.revoke(newer["transfer_id"])["status"] == "REVOKED"
    assert len(first_batch["reports"]) == 5


@pytest.mark.django_db
def test_lost_upload_response_exact_retry_pending_blocks_mail(context, runner, mail, monkeypatch):
    real_call = runner.client.api.call
    attempted = []

    def lose_response(method, path, **kwargs):
        if method == "POST" and path.endswith("/report-submissions"):
            # The durable envelope must exist before this request begins.
            package = kwargs["json_body"]
            assert any(
                runner.load(p.stem)["envelope"] == package for p in runner.root.glob("*.json")
            )
            attempted.append(canonical(package))
            response = real_call(method, path, **kwargs)
            assert response.status == 201
            raise ClientError("Report request: transport failure")
        return real_call(method, path, **kwargs)

    monkeypatch.setattr(runner.client.api, "call", lose_response)
    with pytest.raises(ClientError, match="1 finalized reports pending"):
        runner.send(str(context[1].pk))
    record = runner.load(next(runner.root.glob("*.json")).stem)
    assert record["state"] == "PENDING"
    # A lost response is recoverable by read-only READY confirmation too.
    assert runner.status(record["submission_id"])["state"] == "READY"
    monkeypatch.setattr(runner.client.api, "call", real_call)
    assert runner.retry(record["submission_id"])["http_status"] == 200
    assert canonical(runner.load(record["submission_id"])["envelope"]) == attempted[0]
    ReportSubmission.objects.all().delete()
    with pytest.raises(ClientError, match="1 finalized local reports"):
        runner.link(str(context[1].pk))
    assert mail.send.call_count == 0 and ReportTransfer.objects.count() == 0


@pytest.mark.django_db
def test_confirmed_expired_history_does_not_block_current_ready_reports(context, runner, mail):
    recipient = str(context[1].pk)
    records = runner.send(recipient, storm=True)["reports"]
    historical = records[0]
    ReportSubmission.objects.filter(submission_id=historical["submission_id"]).update(
        expires_at=timezone.now()
    )
    # A locally PENDING record whose server upload completed is resolved by status.
    current = runner.load(records[1]["submission_id"])
    current["state"] = "PENDING"
    runner.save(current)
    original_state = dict(runner.client.state.state)
    original_envelopes = {
        r["submission_id"]: runner.load(r["submission_id"])["envelope"] for r in records
    }
    result = runner.link(recipient)
    assert result["status"] == "SENT" and mail.send.call_count == 1
    transfer = ReportTransfer.objects.get(pk=result["transfer_id"])
    assert transfer.submissions.count() == 4
    assert not transfer.submissions.filter(submission_id=historical["submission_id"]).exists()
    assert runner.load(historical["submission_id"])["state"] == "EXPIRED"
    assert runner.load(current["submission_id"])["state"] == "READY"
    assert len(list(runner.root.glob("*.json"))) == 5
    assert runner.client.state.state == original_state
    assert {
        r["submission_id"]: runner.load(r["submission_id"])["envelope"] for r in records
    } == original_envelopes


@pytest.mark.django_db
@pytest.mark.parametrize(
    "failure", ["pending", "failed", "missing_cached_expired", "network", "malformed"]
)
def test_expired_and_ready_history_never_hide_unconfirmed_uploads(
    context, runner, mail, monkeypatch, failure
):
    recipient = str(context[1].pk)
    records = runner.send(recipient)["reports"]
    historical = records[0]
    ReportSubmission.objects.filter(submission_id=historical["submission_id"]).update(
        expires_at=timezone.now()
    )
    assert runner.status(historical["submission_id"])["state"] == "EXPIRED"
    ready = runner.send(recipient)["reports"][0]
    current = runner.send(recipient)["reports"][0]
    record = runner.load(current["submission_id"])
    unchanged_envelope = record["envelope"]
    record["state"] = (
        "EXPIRED"
        if failure == "missing_cached_expired"
        else "FAILED"
        if failure == "failed"
        else "PENDING"
    )
    runner.save(record)
    if failure in ("pending", "failed", "missing_cached_expired"):
        # Unaccepted/missing server records must not be mistaken for expiry.
        ReportSubmission.objects.filter(submission_id=current["submission_id"]).delete()
    else:
        real_call = runner.client.api.call

        def failed_status(method, path, **kwargs):
            if current["submission_id"] in path:
                if failure == "network":
                    raise ClientError("Report request: transport failure")
                return HttpResponse(200, {}, b'{"state":"EXPIRED"}', path)
            return real_call(method, path, **kwargs)

        monkeypatch.setattr(runner.client.api, "call", failed_status)
    with pytest.raises(ClientError, match="1 finalized local reports"):
        runner.link(recipient)
    assert not mail.send.called and not ReportTransfer.objects.exists()
    assert runner.load(current["submission_id"])["envelope"] == unchanged_envelope
    assert len(list(runner.root.glob("*.json"))) == 3
    assert runner.load(ready["submission_id"])["state"] == "READY"


@pytest.mark.django_db
def test_transfer_filtering_limits_historical_scope_and_no_silent_truncation(
    submitted, mail, report_key, settings
):
    installation, person, tablet, first, _ = submitted
    other = Person.objects.create(
        department=first.department,
        display_name="TEST Other",
        incident_commander_eligible=True,
        incident_commander_email="other@example.test",
        email_verified_at=timezone.now(),
    )
    foreign_recipient = add_submission(submitted, report_key, recipient=other)
    expired = add_submission(submitted, report_key)
    ReportSubmission.objects.filter(pk=expired.pk).update(expires_at=timezone.now())
    legacy = add_submission(submitted, report_key)
    ReportSubmission.objects.filter(pk=legacy.pk).update(document_schema_version=0)
    move_tablet(installation)
    response, _ = issue(submitted, mail)
    assert response.status_code == 201
    transfer = ReportTransfer.objects.get()
    assert list(transfer.submissions.all()) == [first]
    # Same installation and department, historical station: status stays readable.
    assert (
        tablet.get(f"/api/v1/tablet/report-submissions/{first.submission_id}/status").status_code
        == 200
    )
    assert foreign_recipient not in transfer.submissions.all()
    settings.REPORT_TRANSFER_MAX_REPORTS = 1
    # Move back to create a second valid current-scope report.
    from apps.assignments.models import TabletVehicleAssignment
    from apps.organizations.models import Vehicle

    TabletVehicleAssignment.objects.filter(
        tablet=installation.tablet, ended_at__isnull=True
    ).update(ended_at=timezone.now())
    TabletVehicleAssignment.objects.create(
        tablet=installation.tablet,
        vehicle=Vehicle.objects.filter(station=first.station).first(),
        valid_from=timezone.now(),
        created_by=first.department.created_by,
    )
    add_submission(submitted, report_key)
    denied, _ = issue(submitted, mail)
    assert denied.status_code == 409 and denied.json()["code"] == "too_many_reports"
    transfer.refresh_from_db()
    assert transfer.revoked_at is None and mail.send.call_count == 1
    assert person.pk == transfer.recipient_id


@pytest.mark.django_db
def test_chronological_portal_uses_timestamps_not_upload_or_header_keys(context, runner, mail):
    config = runner.client.get_configuration()
    for index in (4, 0, 3, 1, 2):
        _, document, photos = synthetic(index)
        # Arbitrary report types/keys, timezone offsets, tied times have stable UUID ordering.
        document["report_type"] = "new.product.type"
        document["header"][0]["key"] = "arbitrary-key"
        submission_id = str(uuid4())
        wire = envelope(config, str(context[1].pk), document, photos, submission_id)
        assert runner.call("POST", "/report-submissions", wire, (201,))[0] == 201
    _, url = issue(
        (context[0], context[1], context[2], ReportSubmission.objects.first(), None), mail
    )
    browser, _, _ = browser_login(url)
    reports = browser.get("/reports/").context["reports"]
    assert [r["header_fields"][0]["display_value"] for r in reports] == [
        f"TEST-{i:03d}" for i in range(1, 6)
    ]


@pytest.mark.parametrize(
    "command,flag",
    [
        ("send", "recipient-personnel-id"),
        ("storm", "recipient-personnel-id"),
        ("link", "recipient-personnel-id"),
        ("status", "submission-id"),
        ("retry", "submission-id"),
        ("revoke", "transfer-id"),
    ],
)
def test_cli_dispatch_safe_output(command, flag, monkeypatch, tmp_path, capsys):
    client = SimpleNamespace(state=SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr("tools.fake_ipad.cli.build_client", lambda args, out: (None, client))
    method = "send" if command == "storm" else command
    fake = Mock(return_value={"status": "READY"})
    monkeypatch.setattr(ReportTest, method, fake)
    assert (
        main(
            [
                "report-test",
                command,
                "--" + flag,
                str(uuid4()),
                "--state-dir",
                str(tmp_path),
                "--json",
                "--verbose",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "READY"
    fake.assert_called_once()


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("verbose", [False, True])
def test_transport_never_prints_report_payloads(json_mode, verbose, capsys):
    stream = io.StringIO()
    out = Output(json_mode=json_mode, verbose=verbose, stream=stream)
    api = ApiClient("https://firedash.test", out=out, verbose=verbose)
    response = Mock()
    response.status = 201
    response.headers = {"content-type": "application/json"}
    response.read.return_value = b'{"email":"secret@example.test","ciphertext":"secret-ciphertext"}'
    context_manager = Mock()
    context_manager.__enter__ = Mock(return_value=response)
    context_manager.__exit__ = Mock(return_value=False)
    api._opener.open = Mock(return_value=context_manager)
    wire = {
        key: "secret-" + key for key in ("protected", "encrypted_key", "iv", "ciphertext", "tag")
    }
    api.call(
        "POST", "/api/v1/tablet/report-submissions", json_body=wire, bearer="secret-credential"
    )
    out.emit(wire)
    captured = capsys.readouterr()
    text = stream.getvalue() + captured.out + captured.err
    assert "secret-" not in text and "secret@example.test" not in text


@pytest.mark.django_db
@pytest.mark.parametrize(
    "limit,value",
    [
        ("enabled", False),
        ("max_attachments", 2),
        ("max_jpeg_bytes", 10),
        ("max_json_bytes", 10),
        ("max_request_bytes", 10),
        ("max_image_pixels", 10),
        ("public_key_pem", "invalid"),
        ("kid", "invalid/key"),
    ],
)
def test_independent_builder_respects_configuration(context, limit, value):
    config = {**context[3], "report_transfer": {**configuration(), limit: value}}
    _, document, photos = synthetic(0)
    with pytest.raises(ClientError):
        envelope(config, str(context[1].pk), document, photos, str(uuid4()))
    for _, raw in photos:
        with Image.open(io.BytesIO(raw)) as image:
            assert image.format == "JPEG" and image.size == (480, 320)


@pytest.mark.django_db
def test_empty_snapshot_safe_explicit_error(context, mail):
    response = context[2].post(
        API,
        canonical(
            {
                "transfer_request_id": str(uuid4()),
                "recipient_personnel_id": str(context[1].pk),
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 409 and response.json()["code"] == "no_reports_available"
    assert not mail.send.called and not ReportTransfer.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["submission", "snapshot"])
def test_concurrent_acceptance_and_snapshot_share_installation_lock(
    context, runner, mail, monkeypatch, first
):
    from apps.report_transfer import exports

    installation, person, _, _ = context
    runner.send(str(person.pk), storm=True)
    _, document, photos = synthetic(0)
    submission_id = str(uuid4())
    wire = envelope(
        runner.client.get_configuration(), str(person.pk), document, photos, submission_id
    )
    holding, contender, release = Event(), Event(), Event()

    def pause(function):
        def wrapped(*args, **kwargs):
            holding.set()
            assert release.wait(15)
            return function(*args, **kwargs)

        return wrapped

    if first == "submission":
        monkeypatch.setattr(services, "decrypt_and_validate", pause(services.decrypt_and_validate))
        original_lock = transfers.lock_installation

        def lock(pk):
            contender.set()
            return original_lock(pk)

        monkeypatch.setattr(transfers, "lock_installation", lock)
    else:
        monkeypatch.setattr(exports, "load_verified_payload", pause(exports.load_verified_payload))

    def submit():
        if first == "snapshot":
            contender.set()
        return services.submit(installation=installation, envelope=wire)

    def snapshot():
        return transfers.request_transfer(
            installation=installation,
            data={
                "transfer_request_id": str(uuid4()),
                "recipient_personnel_id": str(person.pk),
            },
        )

    def run(function):
        close_old_connections()
        try:
            return function()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run, submit if first == "submission" else snapshot)
        assert holding.wait(15)
        second_future = pool.submit(run, snapshot if first == "submission" else submit)
        try:
            assert contender.wait(15)
        finally:
            release.set()
        first_future.result(timeout=30)
        second_future.result(timeout=30)
    transfer = ReportTransfer.objects.get()
    assert transfer.submissions.count() == (6 if first == "submission" else 5)
    assert transfer.submissions.filter(submission_id=submission_id).exists() == (
        first == "submission"
    )
    assert ReportSubmission.objects.count() == 6 and mail.send.call_count == 1


@pytest.mark.django_db
def test_portal_chronology_normalizes_offsets_and_breaks_ties_by_report_uuid(context, runner, mail):
    config = runner.client.get_configuration()
    expected = []
    for index, stamp in enumerate(
        (
            "2026-10-28T16:00:00+02:00",
            "2026-10-28T14:00:00Z",
            "2026-10-28T13:30:00Z",
        )
    ):
        _, document, photos = synthetic(index)
        document["finalization"]["finalized_at"] = stamp
        wire = envelope(config, str(context[1].pk), document, photos, str(uuid4()))
        runner.call("POST", "/report-submissions", wire, (201,))
        expected.append(document["report_id"])
    outcome = runner.link(str(context[1].pk))
    assert outcome["status"] == "SENT"
    browser, _, _ = browser_login(mail.send.call_args.args[0].body.split()[-1])
    cards = browser.get("/reports/").context["reports"]
    assert [r["report_id"] for r in cards] == [expected[2], *sorted(expected[:2])]
