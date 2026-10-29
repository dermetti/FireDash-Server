"""Separately authorized tablet and portal payload loaders, shared integrity checks."""

import hashlib

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.audit.services import record_event
from apps.report_transfer import storage
from apps.report_transfer.contract import (
    TransferError,
    decode,
    decrypt_and_validate,
    inspect_envelope,
    parse_json,
)
from apps.report_transfer.models import ReportSubmission
from apps.report_transfer.pdf import render_pdf
from apps.report_transfer.services import authorize, effective_expiry


@sensitive_variables()
def load_submission(*, installation, submission_id):
    # Keep authorization, expiry and read consistent with cleanup and reassignment.
    with transaction.atomic():
        installation, vehicle = authorize(installation)
        row = (
            ReportSubmission.objects.select_for_update()
            .filter(
                installation=installation,
                submission_id=submission_id,
                department_id=installation.tablet.department_id,
                station_id=vehicle.station_id,
            )
            .first()
        )
        if row is None:
            raise TransferError("submission_not_found", 404)
        if row.state != "READY" or effective_expiry(row) <= timezone.now():
            raise TransferError("submission_expired", 410)
        return load_verified_payload(row)


@sensitive_variables()
def load_verified_payload(row):
    """Internal primitive: caller MUST authorize the row and guard cleanup/read."""
    if row.document_schema_version != 1:
        raise TransferError("legacy_report_document", 409)
    raw = storage.payload_path(row.payload_key).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row.payload_digest:
        raise TransferError("report_integrity_failure", 409)
    envelope = parse_json(raw)
    header = inspect_envelope(envelope)
    # Historical scope is bound to authenticated ciphertext, never today's
    # station assignment. A changed database reference must fail closed too.
    for field, expected in (
        ("installation_id", row.installation_id),
        ("department_id", row.department_id),
        ("station_id", row.station_id),
        ("recipient_personnel_id", row.recipient_id),
        ("submission_id", row.submission_id),
        ("report_id", row.report_id),
    ):
        if header[field] != str(expected):
            raise TransferError("report_integrity_failure", 409)
    document, attachments, digest = decrypt_and_validate(envelope, header)
    if digest != row.document_digest or document["report_id"] != str(row.report_id):
        raise TransferError("report_integrity_failure", 409)
    return row, document, attachments


def load_portal_submission(request, report_id):
    from apps.report_transfer.transfers import portal_context

    with transaction.atomic():
        _, rows = portal_context(request, report_id=report_id)
        row = ReportSubmission.objects.select_for_update().get(pk=rows[0].pk)
        if row.state != "READY" or effective_expiry(row) <= timezone.now():
            raise TransferError("submission_expired", 410)
        return load_verified_payload(row)


@sensitive_variables()
def render_submission_pdf(*, installation, submission_id):
    row, document, attachments = load_submission(
        installation=installation, submission_id=submission_id
    )
    payload = render_pdf(
        document,
        attachment_hashes={a["id"]: a["sha256"] for a in attachments},
        received_at=row.created_at,
    )
    record_event(
        action="report_transfer.pdf_generated",
        actor_installation_uuid=installation.installation_uuid,
        department=row.department,
        target_type="report_submission",
        target_uuid=row.submission_id,
        metadata={"file_type": "pdf", "installation_id": str(row.installation_id)},
    )
    return payload


@sensitive_variables()
def read_submission_jpeg(*, installation, submission_id, attachment_id):
    _, _, attachments = load_submission(installation=installation, submission_id=submission_id)
    for attachment in attachments:
        if attachment["id"] == str(attachment_id):
            return decode(attachment["data"])
    raise TransferError("attachment_not_found", 404)
