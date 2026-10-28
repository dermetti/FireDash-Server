import copy
import hashlib
import io
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone
from pypdf import PdfReader

from apps.organizations.models import Station
from apps.report_transfer import storage
from apps.report_transfer.apps import report_limits
from apps.report_transfer.contract import TransferError, canonical, decode
from apps.report_transfer.documents import document_digest, validate_document
from apps.report_transfer.exports import read_submission_jpeg, render_submission_pdf
from apps.report_transfer.models import ReportSubmission
from apps.report_transfer.pdf import render_pdf
from apps.report_transfer.services import cleanup, record_successful_download
from apps.report_transfer.tests.test_submissions import (
    URL,
    content,
    context,  # noqa: F401
    encrypt,
    example_document,
    package,
    post,
    report_key,  # noqa: F401
    request_context,  # noqa: F401
)
from apps.tablets.models import AppInstallation


def value(kind, value):
    return {"type": kind, "value": value}


def complete_document():
    ids = [str(uuid4()), str(uuid4())]
    document = example_document(str(uuid4()), ids)
    document["report_type"] = "a-new-report-type-with-no-server-code"
    document["title"] = "Einsatzbericht für Müller & Söhne"
    document["sections"][0]["blocks"].extend(
        [
            {
                "key": "facts",
                "kind": "fields",
                "fields": [
                    {"key": kind, "label": kind, "value": value(kind, v)}
                    for kind, v in [
                        ("text", "plain & literal"),
                        ("integer", 42),
                        ("decimal", "1.25"),
                        ("boolean", True),
                        ("date", "2026-10-28"),
                        ("datetime", "2026-10-28T12:00:00Z"),
                    ]
                ],
            },
            {
                "key": "measurements",
                "kind": "table",
                "columns": [
                    {"key": "name", "label": "Measurement"},
                    {"key": "reading", "label": "Reading"},
                ],
                "rows": [[value("text", "Length"), value("decimal", "12.50")]],
            },
            {"key": "photos", "kind": "attachment_refs", "attachment_ids": ids},
        ]
    )
    document["finalization"]["signature_attachment_id"] = ids[1]
    return document, dict.fromkeys(ids, "a" * 64)


def test_all_content_kinds_and_arbitrary_type():
    document, hashes = complete_document()
    assert (
        validate_document(document, report_id=document["report_id"], attachment_hashes=hashes)
        is document
    )
    now = timezone.now()
    raw = render_pdf(document, attachment_hashes=hashes, received_at=now, exported_at=now)
    pdf = PdfReader(io.BytesIO(raw))
    text = "\n".join(p.extract_text() for p in pdf.pages)
    for expected in [
        document["title"],
        document["report_type"],
        "Report narrative",
        "integer: 42",
        "decimal: 1.25",
        "boolean: Yes",
        "Measurement",
        "12.50",
        "Attachments: 1, 2",
        "1. Evidence",
        "2. Evidence",
        "Signature reference",
        "client-declared",
        "server-observed",
        "Test Author",
        "PDF export",
        now.isoformat(),
    ]:
        assert expected in text
    for page in pdf.pages:
        assert float(page.mediabox.width) == pytest.approx(595.276, abs=0.01)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=0.01)
        assert not page.get("/Annots")
        assert not page.get("/Resources").get("/XObject")
    assert "/OpenAction" not in pdf.trailer["/Root"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "version_bool",
        "version_unknown",
        "type_too_long",
        "title_markup",
        "control",
        "surrogate",
        "bidi",
        "sections_limit",
        "duplicate_section",
        "duplicate_block",
        "unknown_block",
        "unknown_value",
        "integer_bool",
        "integer_large",
        "decimal_float",
        "decimal_exponent",
        "date_invalid",
        "timestamp_naive",
        "timestamp_invalid",
        "author_missing",
        "duplicate_header",
        "duplicate_column",
        "rows_width",
        "too_many_columns",
        "text_limit",
        "attachment_duplicate",
        "attachment_missing",
        "attachment_extra",
        "reference_unknown",
        "reference_duplicate",
        "signature_unknown",
        "wrong_report_id",
        "table_cell_limit",
    ],
)
def test_invalid_documents(fault):
    document, hashes = complete_document()
    blocks = document["sections"][0]["blocks"]
    field = blocks[1]["fields"][0]
    table = blocks[2]
    if fault == "missing":
        del document["finalization"]
    elif fault == "extra":
        document["html"] = "not supported"
    elif fault == "version_bool":
        document["schema_version"] = True
    elif fault == "version_unknown":
        document["schema_version"] = 2
    elif fault == "type_too_long":
        document["report_type"] = "x" * 81
    elif fault == "title_markup":
        document["title"] = '<img src="https://example.test/private"/>'
    elif fault in {"control", "surrogate", "bidi"}:
        document["title"] = {"control": "\x00", "surrogate": "\ud800", "bidi": "\u202e"}[fault]
    elif fault == "sections_limit":
        document["sections"] *= 51
    elif fault == "duplicate_section":
        document["sections"].append(copy.deepcopy(document["sections"][0]))
    elif fault == "duplicate_block":
        blocks[0]["key"] = "summary"
    elif fault == "unknown_block":
        blocks[0]["kind"] = "html"
    elif fault == "unknown_value":
        field["value"] = value("object", {})
    elif fault == "integer_bool":
        field["value"] = value("integer", True)
    elif fault == "integer_large":
        field["value"] = value("integer", 2**53)
    elif fault == "decimal_float":
        field["value"] = value("decimal", 1.25)
    elif fault == "decimal_exponent":
        field["value"] = value("decimal", "1e9")
    elif fault == "date_invalid":
        field["value"] = value("date", "2026-02-30")
    elif fault.startswith("timestamp"):
        document["finalization"]["finalized_at"] = (
            "2026-10-28T12:00:00" if fault.endswith("naive") else "2026-13-01T12:00:00Z"
        )
    elif fault == "author_missing":
        del document["finalization"]["author"]["id"]
    elif fault == "duplicate_header":
        document["header"] *= 2
    elif fault == "duplicate_column":
        table["columns"] *= 2
    elif fault == "rows_width":
        table["rows"][0].pop()
    elif fault == "too_many_columns":
        table["columns"] *= 5
    elif fault == "text_limit":
        blocks[0]["text"] = "x" * 20001
    elif fault == "attachment_duplicate":
        document["attachments"].append(document["attachments"][0])
    elif fault == "attachment_missing":
        document["attachments"].pop()
    elif fault == "attachment_extra":
        document["attachments"].append({"id": str(uuid4()), "title": "Extra", "caption": ""})
    elif fault == "reference_unknown":
        blocks[3]["attachment_ids"] = [str(uuid4())]
    elif fault == "reference_duplicate":
        blocks[3]["attachment_ids"] *= 2
    elif fault == "signature_unknown":
        document["finalization"]["signature_attachment_id"] = str(uuid4())
    elif fault == "wrong_report_id":
        document["report_id"] = "invalid"
    elif fault == "table_cell_limit":
        table["rows"] *= 500
        for i in range(5):
            another = copy.deepcopy(table)
            another["key"] = f"table-{i}"
            blocks.append(another)
    with pytest.raises(TransferError):
        validate_document(document, report_id=document["report_id"], attachment_hashes=hashes)


def test_deterministic_document_integrity():
    document, hashes = complete_document()
    first = document_digest(document, hashes)
    reordered = json.loads(json.dumps(document, sort_keys=True))
    assert document_digest(reordered, dict(reversed(list(hashes.items())))) == first
    expected = {
        "profile": "firedash-report-document-sha256-v1",
        "document": document,
        "attachments": [{"id": i, "sha256": hashes[i]} for i in sorted(hashes)],
    }
    assert first == hashlib.sha256(canonical(expected)).hexdigest()
    changed = copy.deepcopy(document)
    changed["title"] += " revision"
    assert document_digest(changed, hashes) != first
    changed = copy.deepcopy(document)
    changed["sections"][0]["blocks"].reverse()
    assert document_digest(changed, hashes) != first
    changed_hashes = {**hashes, next(iter(hashes)): "b" * 64}
    assert document_digest(document, changed_hashes) != first


def test_pdf_page_overflow_and_single_tall_table_row():
    document, hashes = complete_document()
    blocks = document["sections"][0]["blocks"]
    blocks[0]["text"] = "LONGWORD" * 2500
    table = blocks[2]
    table["columns"] = [{"key": f"c{i}", "label": f"Column {i}"} for i in range(8)]
    table["rows"] = [[value("text", "tall cell " * 400) for _ in range(8)]]
    table["rows"].extend([[value("text", f"row-{i}-end") for _ in range(8)] for i in range(100)])
    raw = render_pdf(document, attachment_hashes=hashes, received_at=timezone.now())
    pages = PdfReader(io.BytesIO(raw)).pages
    assert len(pages) > 5
    assert "row-99-end" in "".join(p.extract_text() for p in pages)
    assert "Page " in pages[-1].extract_text()


def test_pdf_plain_text_entities_and_unicode_are_not_active_markup():
    document = example_document(str(uuid4()))
    document["sections"][0]["blocks"][0]["text"] = (
        '&lt;img src="file:///private"&gt; &amp; https://example.test — 火'
    )
    raw = render_pdf(document, attachment_hashes={}, received_at=timezone.now())
    pdf = PdfReader(io.BytesIO(raw))
    text = "".join(p.extract_text() for p in pdf.pages)
    assert '&lt;img src="file:///private"&gt;' in text
    assert "[U+706B]" in text
    assert all(not p.get("/Annots") for p in pdf.pages)


def test_pdf_oversized_repeated_column_headings_use_flowing_legend():
    document, hashes = complete_document()
    table = document["sections"][0]["blocks"][2]
    table["columns"] = [{"key": f"c{i}", "label": chr(0x706B) * 80} for i in range(8)]
    table["rows"] = [[value("text", "last data cell") for _ in range(8)]]
    raw = render_pdf(document, attachment_hashes=hashes, received_at=timezone.now())
    # A code-point token can wrap across pages; remove intervening page footers
    # before reconstructing the uninterrupted logical content for this assertion.
    text = "".join(
        page.extract_text()
        .replace(f"Report {document['report_id']}\n", "")
        .replace(f"Page {i}\n", "")
        for i, page in enumerate(PdfReader(io.BytesIO(raw)).pages, 1)
    )
    compact = "".join(text.split())
    assert "Column8:" in compact and "lastdatacell" in compact
    assert compact.count("[U+706B]") == 8 * 80


@pytest.mark.django_db
def test_integrated_exports_and_retention_without_download_events(context, report_key):  # noqa: F811
    installation, _, client, header = context
    body = package(header)
    envelope = encrypt(report_key, header, body)
    assert post(client, envelope).status_code == 201
    row = ReportSubmission.objects.get()
    assert row.document_digest and row.document_digest != row.payload_digest
    assert row.document_schema_version == 1
    assert row.expires_at <= row.created_at + timedelta(days=7)
    raw = render_submission_pdf(installation=installation, submission_id=row.submission_id)
    assert raw.startswith(b"%PDF-")
    attachment = body["attachments"][0]
    assert read_submission_jpeg(
        installation=installation, submission_id=row.submission_id, attachment_id=attachment["id"]
    ) == decode(attachment["data"])
    row.refresh_from_db()
    assert row.first_download_at is None
    assert len(list(storage.root().iterdir())) == 1
    assert post(client, envelope).status_code == 200
    with pytest.raises(TransferError, match="attachment_not_found"):
        read_submission_jpeg(
            installation=installation, submission_id=row.submission_id, attachment_id=uuid4()
        )
    ReportSubmission.objects.filter(pk=row.pk).update(document_digest="f" * 64)
    with pytest.raises(TransferError, match="report_integrity_failure"):
        render_submission_pdf(installation=installation, submission_id=row.submission_id)


@pytest.mark.django_db
def test_twelve_jpegs_with_full_arbitrary_document(context, report_key):  # noqa: F811
    installation, _, client, header = context
    body = package(header)
    image = body["attachments"][0]
    body["attachments"] = [{**image, "id": str(uuid4())} for _ in range(12)]
    ids = [a["id"] for a in body["attachments"]]
    document, _ = complete_document()
    document["report_id"] = header["report_id"]
    document["attachments"] = [
        {"id": i, "title": f"Scene {n}", "caption": ""} for n, i in enumerate(ids, 1)
    ]
    document["sections"][0]["blocks"][3]["attachment_ids"] = ids
    document["finalization"]["signature_attachment_id"] = ids[-1]
    body["report"] = {**content(json.dumps(document).encode()), "attachment_ids": ids}
    envelope = encrypt(report_key, header, body)
    assert post(client, envelope).status_code == 201
    assert post(client, envelope).status_code == 200
    raw = render_submission_pdf(installation=installation, submission_id=header["submission_id"])
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages)
    assert "12. Scene 12" in text and document["report_type"] in text


@pytest.mark.django_db
def test_delayed_and_out_of_order_download_recording(context, report_key):  # noqa: F811
    _, _, client, header = context
    assert post(client, encrypt(report_key, header, package(header))).status_code == 201
    row = ReportSubmission.objects.get()
    first = row.created_at + timedelta(hours=1)
    later = first + timedelta(hours=2)
    with patch("apps.report_transfer.services.timezone.now", return_value=later):
        for invalid in (False, "", first.replace(tzinfo=None)):
            with pytest.raises(TransferError):
                record_successful_download(submission_pk=row.pk, completed_at=invalid)
        record_successful_download(submission_pk=row.pk, completed_at=later)
        deadline = record_successful_download(submission_pk=row.pk, completed_at=first)
        assert deadline == first + timedelta(hours=48)
        assert record_successful_download(submission_pk=row.pk, completed_at=later) == deadline
        with pytest.raises(TransferError):
            record_successful_download(
                submission_pk=row.pk, completed_at=later + timedelta(seconds=1)
            )
    row.refresh_from_db()
    assert row.first_download_at == first


@pytest.mark.django_db
@pytest.mark.parametrize("fault", ["legacy", "attachment_mismatch", "wrong_report_id", "html"])
def test_invalid_document_upload(context, report_key, fault):  # noqa: F811
    _, _, client, header = context
    body = package(header)
    document = json.loads(decode(body["report"]["data"]))
    if fault == "legacy":
        document = {"opaque": "old development report"}
    elif fault == "attachment_mismatch":
        document["attachments"] = []
    elif fault == "wrong_report_id":
        document["report_id"] = str(uuid4())
    else:
        document["sections"][0]["blocks"][0]["text"] = "<script>alert(1)</script>"
    body["report"].update(content(json.dumps(document).encode()))
    assert post(client, encrypt(report_key, header, body)).status_code == 400
    assert not ReportSubmission.objects.exists()


@pytest.mark.django_db
def test_legacy_exact_replay_is_preserved_without_invented_metadata(context, report_key):  # noqa: F811
    installation, person, client, header = context
    body = package(header)
    body["report"].update(content(b'{"legacy":"opaque"}'))
    envelope = encrypt(report_key, header, body)
    raw = canonical(envelope)
    row = ReportSubmission.objects.create(
        installation=installation,
        recipient=person,
        department_id=header["department_id"],
        station_id=header["station_id"],
        submission_id=header["submission_id"],
        report_id=header["report_id"],
        payload_digest=hashlib.sha256(raw).hexdigest(),
        payload_key=storage.write_payload(raw),
        key_version="test-1",
        expires_at=timezone.now() + timedelta(hours=72),
    )
    assert post(client, envelope).status_code == 200
    assert client.get(f"{URL}/{row.submission_id}/status").status_code == 200
    with pytest.raises(TransferError, match="legacy_report_document"):
        render_submission_pdf(installation=installation, submission_id=row.submission_id)
    assert row.document_digest == "" and row.document_schema_version is None


@pytest.mark.django_db
@pytest.mark.parametrize("elapsed", [1, 6])
def test_first_explicit_download_shortens_retention_once(context, report_key, elapsed):  # noqa: F811
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    assert post(client, envelope).status_code == 201
    row = ReportSubmission.objects.get()
    completed = row.created_at + timedelta(days=elapsed)
    original_expiry = row.expires_at
    AppInstallation.objects.filter(pk=row.installation_id).update(
        authorization_valid_until=row.created_at + timedelta(days=8)
    )
    with patch("apps.report_transfer.services.timezone.now", return_value=completed):
        deadline = record_successful_download(submission_pk=row.pk)
    assert deadline == min(original_expiry, completed + timedelta(hours=48))
    with patch(
        "apps.report_transfer.services.timezone.now", return_value=completed + timedelta(hours=1)
    ):
        assert record_successful_download(submission_pk=row.pk) == deadline
    row.refresh_from_db()
    assert row.first_download_at == completed
    with patch("apps.report_transfer.services.timezone.now", return_value=deadline):
        assert post(client, envelope).json()["state"] == "EXPIRED"
        with pytest.raises(TransferError, match="submission_expired"):
            record_successful_download(submission_pk=row.pk)
    assert cleanup(now=deadline)["expired"] == 1
    assert not storage.payload_path(row.payload_key).exists()


@pytest.mark.django_db
def test_expiry_ceiling_enforced_even_for_long_legacy_deadline(context, report_key):  # noqa: F811
    _, _, client, header = context
    assert post(client, encrypt(report_key, header, package(header))).status_code == 201
    row = ReportSubmission.objects.get()
    ReportSubmission.objects.filter(pk=row.pk).update(
        expires_at=row.created_at + timedelta(days=90)
    )
    now = row.created_at + timedelta(days=7)
    AppInstallation.objects.filter(pk=row.installation_id).update(
        authorization_valid_until=now + timedelta(days=1)
    )
    with patch("apps.report_transfer.services.timezone.now", return_value=now):
        assert client.get(f"{URL}/{row.submission_id}/status").json()["state"] == "EXPIRED"
    assert cleanup(now=now)["expired"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("fault", ["revoked", "station", "installation", "expired", "ciphertext"])
def test_export_access_and_ciphertext_integrity(context, report_key, fault):  # noqa: F811
    installation, _, client, header = context
    assert post(client, encrypt(report_key, header, package(header))).status_code == 201
    row = ReportSubmission.objects.get()
    expected = "submission_not_found"
    if fault == "revoked":
        installation.status = AppInstallation.Status.REVOKED
        installation.revoked_at = timezone.now()
        installation.save()
        expected = "installation_not_authorized"
    elif fault == "station":
        station = Station.objects.create(department=row.department, name="Other", short_code="O")
        ReportSubmission.objects.filter(pk=row.pk).update(station=station)
    elif fault == "installation":
        row.submission_id = uuid4()
    elif fault == "expired":
        ReportSubmission.objects.filter(pk=row.pk).update(expires_at=timezone.now())
        expected = "submission_expired"
    else:
        storage.payload_path(row.payload_key).write_bytes(b"tampered")
        expected = "report_integrity_failure"
    with pytest.raises(TransferError, match=expected):
        render_submission_pdf(installation=installation, submission_id=row.submission_id)


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAX_ATTACHMENTS", 33),
        ("RETENTION_HOURS", 169),
        ("MAX_REQUEST_BYTES", 129 * 1024 * 1024),
    ],
)
def test_configuration_upper_bounds(settings, name, value):
    setattr(settings, f"REPORT_TRANSFER_{name}", value)
    assert "report_transfer.E002" in [e.id for e in report_limits(None)]
