"""Atomic acceptance and retryable expiry, separate from publications."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.outbound_mail.report_admission import ReportAdmissionError, resolve_report_recipient
from apps.publications.manifests import ManifestError, control_plane_context
from apps.report_transfer import storage
from apps.report_transfer.contract import (
    TransferError,
    canonical,
    decrypt_and_validate,
    inspect_envelope,
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
            decrypt_and_validate(envelope, header)
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
                payload_key=payload_key,
                key_version=header["kid"],
                expires_at=timezone.now()
                + timedelta(hours=settings.REPORT_TRANSFER_RETENTION_HOURS),
            )
        return result(row), True
    except BaseException:
        if payload_key:
            storage.remove_payload(payload_key)
        raise


def result(row):
    state = "EXPIRED" if row.expires_at <= timezone.now() else row.state
    return {
        "submission_id": str(row.submission_id),
        "report_id": str(row.report_id),
        "state": state,
        "expires_at": row.expires_at.isoformat(),
    }


def status(*, installation, submission_id):
    installation, vehicle = authorize(installation)
    row = ReportSubmission.objects.filter(
        installation=installation,
        submission_id=submission_id,
        department_id=installation.tablet.department_id,
        station_id=vehicle.station_id,
    ).first()
    if row is None:
        raise TransferError("submission_not_found", 404)
    return result(row)


def cleanup(*, now=None):
    now = now or timezone.now()
    expired = failed = orphans = 0
    # One transaction per expired row: an unlink failure preserves the reference
    # for retry; a crash after unlink is safe because missing files are success.
    for pk in (
        ReportSubmission.objects.filter(expires_at__lte=now, state="READY")
        .values_list("pk", flat=True)
        .iterator()
    ):
        try:
            with transaction.atomic():
                storage.lock_spool()
                row = ReportSubmission.objects.select_for_update().get(pk=pk)
                if row.state != "READY":
                    continue
                storage.remove_payload(row.payload_key)
                row.payload_key = ""
                row.state = "EXPIRED"
                row.save(update_fields=("payload_key", "state"))
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
    return {"expired": expired, "orphans": orphans, "failed": failed}
