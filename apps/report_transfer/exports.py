"""Authorized in-memory exports for later portal use; no HTTP/download events."""

import hashlib

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

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
        if row.document_schema_version != 1:
            raise TransferError("legacy_report_document", 409)
        raw = storage.payload_path(row.payload_key).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row.payload_digest:
            raise TransferError("report_integrity_failure", 409)
        envelope = parse_json(raw)
        header = inspect_envelope(envelope)
        document, attachments, digest = decrypt_and_validate(envelope, header)
        if digest != row.document_digest or document["report_id"] != str(row.report_id):
            raise TransferError("report_integrity_failure", 409)
        return row, document, attachments


@sensitive_variables()
def render_submission_pdf(*, installation, submission_id):
    row, document, attachments = load_submission(
        installation=installation, submission_id=submission_id
    )
    return render_pdf(
        document,
        attachment_hashes={a["id"]: a["sha256"] for a in attachments},
        received_at=row.created_at,
    )


@sensitive_variables()
def read_submission_jpeg(*, installation, submission_id, attachment_id):
    _, _, attachments = load_submission(installation=installation, submission_id=submission_id)
    for attachment in attachments:
        if attachment["id"] == str(attachment_id):
            return decode(attachment["data"])
    raise TransferError("attachment_not_found", 404)
