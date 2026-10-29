"""Atomic acceptance and retryable expiry, separate from publications."""

import hashlib
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.outbound_mail.report_admission import ReportAdmissionError, resolve_report_recipient
from apps.publications.manifests import ManifestError, control_plane_context
from apps.report_transfer import storage
from apps.report_transfer.contract import (
    TransferError,
    canonical,
    decrypt_and_validate,
    inspect_envelope,
    require,
)
from apps.report_transfer.models import ReportSubmission
from apps.tablets.models import AppInstallation


def authorize(installation):
    try:
        installation, vehicle = control_plane_context(installation=installation, now=timezone.now())
    except ManifestError:
        raise TransferError("installation_not_authorized", 403) from None
    if (
        installation.tablet.status != installation.tablet.Status.ACTIVE
        or vehicle is None
        or vehicle.station.department_id != installation.tablet.department_id
    ):
        raise TransferError("installation_not_authorized", 403)
    return installation, vehicle


@sensitive_variables()
def submit(*, installation, envelope):
    header = inspect_envelope(envelope)
    installation, vehicle = authorize(installation)
    if (
        header["installation_id"] != str(installation.pk)
        or header["department_id"] != str(installation.tablet.department_id)
        or header["station_id"] != str(vehicle.station_id)
    ):
        raise TransferError("scope_not_authorized", 403)
    raw = canonical(envelope)
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > settings.REPORT_TRANSFER_MAX_REQUEST_BYTES:
        raise TransferError("package_too_large", 413)
    payload_key = None
    try:
        with transaction.atomic():
            AppInstallation.objects.select_for_update().get(pk=installation.pk)
            installation, vehicle = authorize(installation)
            if header["department_id"] != str(installation.tablet.department_id) or header[
                "station_id"
            ] != str(vehicle.station_id):
                raise TransferError("scope_not_authorized", 403)
            try:
                resolve_report_recipient(
                    department=installation.tablet.department,
                    recipient_personnel_id=header["recipient_personnel_id"],
                )
            except ReportAdmissionError:
                raise TransferError("recipient_not_authorized", 403) from None
            existing = (
                ReportSubmission.objects.filter(installation=installation)
                .filter(Q(submission_id=header["submission_id"]) | Q(report_id=header["report_id"]))
                .first()
            )
            if existing:
                if (
                    str(existing.submission_id) != header["submission_id"]
                    or existing.payload_digest != digest
                ):
                    raise TransferError("idempotency_conflict", 409)
                return result(existing), False
            _, _, document_digest = decrypt_and_validate(envelope, header)
            storage.lock_spool()
            payload_key = storage.write_payload(raw)
            row = ReportSubmission.objects.create(
                installation=installation,
                submission_id=header["submission_id"],
                report_id=header["report_id"],
                recipient_id=header["recipient_personnel_id"],
                department_id=installation.tablet.department_id,
                station_id=vehicle.station_id,
                payload_digest=digest,
                document_digest=document_digest,
                document_schema_version=1,
                payload_key=payload_key,
                key_version=header["kid"],
                expires_at=timezone.now()
                + timedelta(hours=min(settings.REPORT_TRANSFER_RETENTION_HOURS, 168)),
            )
            record_event(
                action="report_transfer.submission_accepted",
                actor_installation_uuid=installation.installation_uuid,
                department=row.department,
                station=row.station,
                target_type="report_submission",
                target_uuid=row.submission_id,
                metadata={
                    "report_id": str(row.report_id),
                    "installation_id": str(row.installation_id),
                    "accepted_at": row.created_at.isoformat(),
                },
            )
        return result(row), True
    except BaseException:
        if payload_key:
            storage.remove_payload(payload_key)
        raise


def result(row):
    state = "EXPIRED" if effective_expiry(row) <= timezone.now() else row.state
    return {
        "submission_id": str(row.submission_id),
        "report_id": str(row.report_id),
        "state": state,
        "expires_at": effective_expiry(row).isoformat(),
    }


def status(*, installation, submission_id):
    installation, _ = authorize(installation)
    row = ReportSubmission.objects.filter(
        installation=installation,
        submission_id=submission_id,
        department_id=installation.tablet.department_id,
    ).first()
    if row is None:
        raise TransferError("submission_not_found", 404)
    return result(row)


def effective_expiry(row):
    deadline = min(row.expires_at, row.created_at + timedelta(days=7))
    if row.first_download_at:
        deadline = min(deadline, row.first_download_at + timedelta(hours=48))
    for completed_at in storage.pending_downloads(row.pk):
        if completed_at >= row.created_at:
            deadline = min(deadline, completed_at + timedelta(hours=48))
    return deadline


def record_successful_download(*, submission_pk, completed_at=None, download_audit=None):
    """Trusted portal hook AFTER a completed explicit download, never a preview.

    The caller must authorize the resource before serving it. This is intentionally
    not an HTTP endpoint and does not grant access or manufacture delivery events.
    """
    with transaction.atomic():
        row = ReportSubmission.objects.select_for_update().get(pk=submission_pk)
        now = timezone.now()
        if completed_at is None:
            completed_at = now
        require(isinstance(completed_at, datetime) and timezone.is_aware(completed_at))
        require(row.created_at <= completed_at <= now)
        if row.state != "READY" or effective_expiry(row) <= completed_at:
            raise TransferError("submission_expired", 410)
        if row.first_download_at is None or completed_at < row.first_download_at:
            row.first_download_at = completed_at
        row.expires_at = effective_expiry(row)
        row.save(update_fields=("first_download_at", "expires_at"))
        if (
            download_audit
            and not AuditEvent.objects.filter(
                action="report_transfer.download_completed",
                target_uuid=row.submission_id,
                metadata__download_id=download_audit["download_id"],
            ).exists()
        ):
            record_event(
                action="report_transfer.download_completed",
                department=row.department,
                target_type="report_submission",
                target_uuid=row.submission_id,
                metadata={
                    **download_audit,
                    "installation_id": str(row.installation_id),
                    "completed_at": completed_at.isoformat(),
                },
            )
        return row.expires_at


def cleanup(*, now=None):
    now = now or timezone.now()
    expired = failed = orphans = 0
    # Retry only durable evidence of an already completed explicit download.
    # This never fabricates an event for previews or interrupted streams.
    for path in storage.root().glob("*.receipt"):
        try:
            pk, completed_at, download_audit = storage.read_download_receipt(
                path, include_audit=True
            )
            row = ReportSubmission.objects.filter(pk=pk).first()
            if row is not None and row.state == "READY":
                record_successful_download(
                    submission_pk=pk, completed_at=completed_at, download_audit=download_audit
                )
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (OSError, TransferError):
            failed += 1
    # One transaction per expired row: an unlink failure preserves the reference
    # for retry; a crash after unlink is safe because missing files are success.
    for pk in (
        ReportSubmission.objects.filter(state="READY")
        .filter(
            Q(expires_at__lte=now)
            | Q(created_at__lte=now - timedelta(days=7))
            | Q(first_download_at__lte=now - timedelta(hours=48))
        )
        .values_list("pk", flat=True)
        .iterator()
    ):
        try:
            with transaction.atomic():
                storage.lock_spool()
                row = ReportSubmission.objects.select_for_update().get(pk=pk)
                if row.state != "READY" or effective_expiry(row) > now:
                    continue
                storage.remove_payload(row.payload_key)
                row.payload_key = ""
                row.state = "EXPIRED"
                row.save(update_fields=("payload_key", "state"))
                record_event(
                    action="report_transfer.content_purged",
                    department=row.department,
                    station=row.station,
                    target_type="report_submission",
                    target_uuid=row.submission_id,
                    metadata={"result": "expired", "installation_id": str(row.installation_id)},
                )
                expired += 1
        except (OSError, TransferError):
            failed += 1
    with transaction.atomic():
        storage.lock_spool()
        referenced = set(
            ReportSubmission.objects.exclude(payload_key="").values_list("payload_key", flat=True)
        )
        for path in storage.root().glob("*.jwe"):
            if path.name not in referenced:
                try:
                    storage.remove_payload(path.name)
                    orphans += 1
                except (OSError, TransferError):
                    failed += 1
        for path in storage.root().glob("*.receipt-tmp"):
            try:
                if path.stat().st_mtime < (now - timedelta(hours=1)).timestamp():
                    path.unlink(missing_ok=True)
                    orphans += 1
            except OSError:
                failed += 1
    return {"expired": expired, "orphans": orphans, "failed": failed}
