"""Durable, installation-scoped idempotency for synchronous report delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.outbound_mail.models import TabletReportDeliveryRequest
from apps.outbound_mail.report_delivery import (
    REPORT_ATTACHMENT_FILENAME,
    ReportDeliveryCode,
    ReportDeliveryOutcome,
    deliver_outbound_report,
)
from apps.tablets.models import AppInstallation


@dataclass(frozen=True)
class TabletReportDeliveryResult:
    state: str
    code: str
    recipient_email: str | None = None
    accepted_at: datetime | None = None

    @property
    def delivered(self) -> bool:
        return self.state == TabletReportDeliveryRequest.State.SUCCESS


def submit_tablet_report_delivery(
    *,
    installation: AppInstallation,
    delivery_request_id: UUID,
    recipient_personnel_id: UUID,
    pdf: bytes | BinaryIO,
) -> TabletReportDeliveryResult:
    request, created = _claim_or_replay(
        installation=installation,
        delivery_request_id=delivery_request_id,
        recipient_personnel_id=recipient_personnel_id,
    )
    if not created:
        return _replay(request=request, recipient_personnel_id=recipient_personnel_id)

    # The claim is committed before network I/O. A process crash from this
    # point forward intentionally leaves PROCESSING, replayed as UNKNOWN.
    outcome = deliver_outbound_report(
        installation=installation,
        recipient_personnel_id=recipient_personnel_id,
        pdf=pdf,
        filename=REPORT_ATTACHMENT_FILENAME,
    )
    state = _terminal_state(outcome)
    with transaction.atomic():
        request = TabletReportDeliveryRequest.objects.select_for_update().get(pk=request.pk)
        request.state = state
        request.result_code = outcome.code
        request.recipient_email = outcome.recipient_email or ""
        request.completed_at = timezone.now()
        request.save(update_fields=("state", "result_code", "recipient_email", "completed_at"))
    return TabletReportDeliveryResult(
        state=state,
        code=outcome.code,
        recipient_email=request.recipient_email or None,
        accepted_at=request.completed_at,
    )


def _claim_or_replay(*, installation, delivery_request_id, recipient_personnel_id):
    try:
        with transaction.atomic():
            request, created = TabletReportDeliveryRequest.objects.get_or_create(
                installation=installation,
                delivery_request_id=delivery_request_id,
                defaults={
                    "department": installation.tablet.department,
                    "recipient_personnel_id": recipient_personnel_id,
                },
            )
            return request, created
    except IntegrityError:
        # A concurrent claimant committed first. Never retry a provider call.
        return (
            TabletReportDeliveryRequest.objects.get(
                installation=installation, delivery_request_id=delivery_request_id
            ),
            False,
        )


def _replay(*, request, recipient_personnel_id) -> TabletReportDeliveryResult:
    if request.recipient_personnel_id != recipient_personnel_id:
        return TabletReportDeliveryResult(state="CONFLICT", code="idempotency_conflict")
    if request.state == TabletReportDeliveryRequest.State.PROCESSING:
        return TabletReportDeliveryResult(state="UNKNOWN", code="delivery_indeterminate")
    return TabletReportDeliveryResult(
        state=request.state,
        code=request.result_code,
        recipient_email=(request.recipient_email or None) if request.state == "SUCCESS" else None,
        accepted_at=request.completed_at if request.state == "SUCCESS" else None,
    )


def _terminal_state(outcome: ReportDeliveryOutcome) -> str:
    if outcome.delivered:
        return TabletReportDeliveryRequest.State.SUCCESS
    if outcome.code == ReportDeliveryCode.PROVIDER_UNAVAILABLE:
        return TabletReportDeliveryRequest.State.UNKNOWN
    return TabletReportDeliveryRequest.State.FAILED
