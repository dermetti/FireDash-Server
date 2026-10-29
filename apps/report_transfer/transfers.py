"""Generation-scoped grants, one-attempt mail and server-side portal sessions."""

import hashlib
import hmac
import secrets
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.outbound_mail.report_admission import ReportAdmissionError, resolve_report_recipient
from apps.outbound_mail.report_delivery import recipient_to_mail_address
from apps.outbound_mail.resolution import resolve_department_mail_provider
from apps.outbound_mail.runtime import MailSendResult, MessageRejectedError, OutboundMessage
from apps.report_transfer.contract import TransferError, canonical, require
from apps.report_transfer.models import ReportPortalSession, ReportSubmission, ReportTransfer
from apps.report_transfer.services import authorize, effective_expiry
from apps.tablets.models import AppInstallation


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def email_digest(value):
    # A keyed fingerprint binds the grant to the verified mailbox at issuance.
    return hmac.new(
        settings.SECRET_KEY.encode(), ("report-recipient:" + value).encode(), hashlib.sha256
    ).hexdigest()


def lock_installation(pk):
    return AppInstallation.objects.select_for_update().get(pk=pk)


def recipient_for(installation, person_id):
    try:
        return resolve_report_recipient(
            department=installation.tablet.department, recipient_personnel_id=person_id
        )
    except ReportAdmissionError:
        raise TransferError("recipient_not_authorized", 403) from None


def check_transfer(row):
    if row.revoked_at or row.mail_state != "SENT":
        raise TransferError("access_unavailable", 403)
    # Issued grants outlive operational leases/assignments, but never explicit
    # asset, installation or department revocation. Always reload persisted state.
    installation = AppInstallation.objects.select_related("tablet__department").get(
        pk=row.installation_id
    )
    if (
        installation.status not in (AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
        or installation.revoked_at is not None
        or not installation.tablet.active
        or installation.tablet.status != installation.tablet.Status.ACTIVE
        or installation.tablet.department.status != installation.tablet.department.Status.ACTIVE
    ):
        raise TransferError("access_unavailable", 403)
    recipient = recipient_for(installation, row.recipient_id)
    if not hmac.compare_digest(email_digest(recipient.email), row.recipient_email_digest):
        raise TransferError("access_unavailable", 403)
    return installation


def check_report(row, transfer, installation, vehicle=None):
    if (
        row.installation_id != installation.pk
        or row.recipient_id != transfer.recipient_id
        or row.department_id != installation.tablet.department_id
        or (vehicle is not None and row.station_id != vehicle.station_id)
        or row.document_schema_version != 1
        or row.state != "READY"
        or effective_expiry(row) <= timezone.now()
    ):
        raise TransferError("report_unavailable", 404)


def transfer_result(row):
    # No bearer material, mailbox or report contents in tablet responses.
    return {"transfer_id": str(row.pk), "status": "REVOKED" if row.revoked_at else row.mail_state}


@sensitive_variables()
def request_transfer(*, installation, data):
    require(isinstance(data, dict) and set(data) == {"transfer_request_id", "submission_ids"})
    try:
        request_id = UUID(data["transfer_request_id"])
        ids = data["submission_ids"]
        require(isinstance(ids, list) and 1 <= len(ids) <= 20)
        ids = [UUID(i) for i in ids]
        require(len(set(ids)) == len(ids))
    except (ValueError, TypeError, AttributeError):
        raise TransferError("invalid_package", 400) from None
    fingerprint = hashlib.sha256(canonical(sorted(str(i) for i in ids))).hexdigest()
    installation, _ = authorize(installation)
    with transaction.atomic():
        lock_installation(installation.pk)
        installation, vehicle = authorize(installation)
        prior = ReportTransfer.objects.filter(
            installation=installation, transfer_request_id=request_id
        ).first()
        if prior:
            if prior.request_digest != fingerprint:
                raise TransferError("idempotency_conflict", 409)
            return transfer_result(prior), False
        rows = list(
            ReportSubmission.objects.select_for_update().filter(
                installation=installation, submission_id__in=ids
            )
        )
        if len(rows) != len(ids) or len({r.recipient_id for r in rows}) != 1:
            raise TransferError("reports_not_authorized", 403)
        recipient = recipient_for(installation, rows[0].recipient_id)
        for row in rows:
            check_report(row, row, installation, vehicle)
        now = timezone.now()
        attempts = ReportTransfer.objects.filter(installation=installation)
        # Three immediate replacements remain possible; no cooldown on revocation.
        if (
            attempts.filter(issued_at__gt=now - timedelta(hours=1)).count()
            >= settings.REPORT_TRANSFER_MAILS_PER_HOUR
            or attempts.filter(
                recipient_id=recipient.personnel_id, issued_at__gt=now - timedelta(minutes=1)
            ).count()
            >= settings.REPORT_TRANSFER_MAILS_PER_MINUTE
        ):
            raise TransferError("transfer_rate_limited", 429)
        from config.settings.base import validate_public_origin

        try:
            validate_public_origin(settings.FIREDASH_PUBLIC_ORIGIN)
        except (RuntimeError, ValueError):
            raise TransferError("portal_unavailable", 503) from None
        token = secrets.token_urlsafe(32)
        previous = ReportTransfer.objects.filter(
            installation=installation, recipient_id=recipient.personnel_id, revoked_at__isnull=True
        )
        previous_ids = list(previous.values_list("pk", flat=True))
        previous.update(revoked_at=now)
        for previous_id in previous_ids:
            record_event(
                action="report_transfer.revoked",
                actor_installation_uuid=installation.installation_uuid,
                department=installation.tablet.department,
                target_type="report_transfer",
                target_uuid=previous_id,
                metadata={"source": "replacement", "revoked_at": now.isoformat()},
            )
        transfer = ReportTransfer.objects.create(
            installation=installation,
            recipient_id=recipient.personnel_id,
            transfer_request_id=request_id,
            request_digest=fingerprint,
            recipient_email_digest=email_digest(recipient.email),
            token_digest=digest(token),
            issued_at=now,
            token_expires_at=now + timedelta(minutes=60),
        )
        transfer.submissions.set(rows)
        record_event(
            action="report_transfer.created",
            actor_installation_uuid=installation.installation_uuid,
            department=installation.tablet.department,
            target_type="report_transfer",
            target_uuid=transfer.pk,
            metadata={"issued_at": now.isoformat()},
        )
    # Claim committed before any provider I/O. A crash remains PROCESSING;
    # neither replay nor a worker may retry this uncertain send.
    state, code = send_link(transfer.pk, token)
    with transaction.atomic():
        ReportTransfer.objects.filter(pk=transfer.pk).update(mail_state=state, mail_code=code)
        record_event(
            action="report_transfer.link_issued",
            actor_installation_uuid=installation.installation_uuid,
            department=installation.tablet.department,
            target_type="report_transfer",
            target_uuid=transfer.pk,
            metadata={"result": state, "code": code},
        )
    transfer.refresh_from_db()
    return transfer_result(transfer), True


@sensitive_variables()
def send_link(transfer_id, token):
    try:
        row = ReportTransfer.objects.select_related("installation__tablet__department").get(
            pk=transfer_id
        )
        if row.revoked_at or row.token_expires_at <= timezone.now():
            return "FAILED", "access_unavailable"
        installation, _ = authorize(row.installation)
        recipient = recipient_for(installation, row.recipient_id)
        if email_digest(recipient.email) != row.recipient_email_digest:
            return "FAILED", "recipient_changed"
        resolution = resolve_department_mail_provider(department=installation.tablet.department)
        if not resolution.is_usable:
            return "FAILED", "provider_unavailable"
        provider = resolution.provider
        message = OutboundMessage(
            sender=provider.sender_identity,
            recipient=recipient_to_mail_address(recipient.email),
            subject="FireDash – Sicherer Zugang",
            body="Für Sie wurde ein temporärer Zugang bereitgestellt. "
            "Der Link ist 60 Minuten gültig und kann einmal zur Anmeldung verwendet werden.\n\n"
            + settings.FIREDASH_PUBLIC_ORIGIN.rstrip("/")
            + "/reports/access/"
            + token
            + "/",
        )
        try:
            result = provider.send(message)
        except MessageRejectedError:
            return "FAILED", "message_rejected"
        except Exception:
            return "UNKNOWN", "delivery_indeterminate"
        if not isinstance(result, MailSendResult):
            return "UNKNOWN", "delivery_indeterminate"
        return "SENT", "delivered"
    except Exception:
        # Never log adapter exception text (possibly containing link/credentials).
        return "FAILED", "delivery_unavailable"


def revoke_tablet(*, installation, transfer_id):
    installation, _ = authorize(installation)
    with transaction.atomic():
        lock_installation(installation.pk)
        installation, _ = authorize(installation)
        row = ReportTransfer.objects.filter(pk=transfer_id, installation=installation).first()
        if row is None:
            raise TransferError("transfer_not_found", 404)
        if row.revoked_at is None:
            row.revoked_at = timezone.now()
            row.save(update_fields=("revoked_at",))
            record_event(
                action="report_transfer.revoked",
                actor_installation_uuid=installation.installation_uuid,
                department=installation.tablet.department,
                target_type="report_transfer",
                target_uuid=row.pk,
                metadata={"source": "tablet", "revoked_at": row.revoked_at.isoformat()},
            )
        return transfer_result(row)


@sensitive_variables()
def token_transfer(token):
    if not isinstance(token, str) or len(token) != 43:
        raise TransferError("access_unavailable", 403)
    row = (
        ReportTransfer.objects.select_related("installation__tablet__department")
        .filter(token_digest=digest(token))
        .first()
    )
    if (
        row is None
        or row.token_expires_at <= timezone.now()
        or row.revoked_at
        or row.mail_state != "SENT"
    ):
        raise TransferError("access_unavailable", 403)
    # Possession of a used token remains sufficient to revoke this generation
    # even when recipient eligibility or the installation lease has changed.
    if row.consumed_at is None:
        check_transfer(row)
    return row


@sensitive_variables()
def redeem(request, token):
    try:
        _redeem(request, token)
    except TransferError as error:
        # No attacker-chosen token, IP, UA or arbitrary text is retained. Known
        # generations get at most one failure; unknown tokens share a global
        # one-minute audit window across workers, without blocking on its lock.
        candidate = (
            ReportTransfer.objects.filter(token_digest=digest(token)).first()
            if isinstance(token, str) and len(token) == 43
            else None
        )
        if candidate:
            with transaction.atomic():
                lock_installation(candidate.installation_id)
                if not AuditEvent.objects.filter(
                    action="report_transfer.login_failed", target_uuid=candidate.pk
                ).exists():
                    record_event(
                        action="report_transfer.login_failed",
                        target_type="report_transfer",
                        target_uuid=candidate.pk,
                        metadata={"code": error.code},
                    )
        else:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [7361031002])
                    acquired = cursor.fetchone()[0]
                if (
                    acquired
                    and not AuditEvent.objects.filter(
                        action="report_transfer.login_failed",
                        target_uuid__isnull=True,
                        timestamp__gte=timezone.now() - timedelta(minutes=1),
                    ).exists()
                ):
                    record_event(
                        action="report_transfer.login_failed",
                        target_type="report_transfer",
                        metadata={"code": "unidentified_token"},
                    )
        raise


@sensitive_variables()
def _redeem(request, token):
    candidate = token_transfer(token)
    with transaction.atomic():
        lock_installation(candidate.installation_id)
        row = token_transfer(token)
        if row.consumed_at:
            raise TransferError("access_used", 409)
        installation = check_transfer(row)
        available = False
        for report in row.submissions.all():
            try:
                check_report(report, row, installation)
            except TransferError:
                continue
            available = True
        if not available:
            raise TransferError("access_unavailable", 403)
        # Flush also removes any administrator identity: portal grants never carry
        # general FireDash rights. Dedicated server-side session key is rotated.
        request.session.flush()
        request.session.create()
        portal = ReportPortalSession.objects.create(
            transfer=row,
            session_digest=digest(request.session.session_key),
            last_seen_at=timezone.now(),
        )
        request.session["report_portal"] = str(portal.pk)
        request.session.set_expiry(1800)
        row.consumed_at = timezone.now()
        row.save(update_fields=("consumed_at",))
        record_event(
            action="report_transfer.login_succeeded",
            department=installation.tablet.department,
            target_type="report_transfer",
            target_uuid=row.pk,
            metadata={"consumed_at": row.consumed_at.isoformat()},
        )


@sensitive_variables()
def revoke_token(token):
    candidate = token_transfer(token)
    with transaction.atomic():
        lock_installation(candidate.installation_id)
        # Reload under the same lock used for new issuance. Never target recipient
        # or 'current generation': only this token's immutable transfer UUID.
        row = token_transfer(token)
        if row.consumed_at is None:
            raise TransferError("access_unavailable", 403)
        row.revoked_at = timezone.now()
        row.save(update_fields=("revoked_at",))
        record_event(
            action="report_transfer.revoked",
            target_type="report_transfer",
            target_uuid=row.pk,
            metadata={"source": "consumed_token", "revoked_at": row.revoked_at.isoformat()},
        )


def portal_context(request, *, report_id=None):
    """Reauthorize actual recipient grant; no tablet-export impersonation."""
    key = request.session.get("report_portal")
    try:
        pk = UUID(key) if isinstance(key, str) else None
    except ValueError:
        pk = None
    candidate = ReportPortalSession.objects.filter(pk=pk).first()
    if candidate is None or not request.session.session_key:
        raise TransferError("access_unavailable", 403)
    with transaction.atomic():
        transfer = ReportTransfer.objects.get(pk=candidate.transfer_id)
        lock_installation(transfer.installation_id)
        portal = (
            ReportPortalSession.objects.select_for_update(of=("self",))
            .select_related("transfer__installation__tablet__department")
            .get(pk=pk)
        )
        transfer = portal.transfer
        now = timezone.now()
        if (
            portal.logged_out_at
            or portal.last_seen_at + timedelta(minutes=30) <= now
            or not hmac.compare_digest(portal.session_digest, digest(request.session.session_key))
        ):
            raise TransferError("access_unavailable", 403)
        installation = check_transfer(transfer)
        if transfer.consumed_at is None:
            raise TransferError("access_unavailable", 403)
        rows = list(
            transfer.submissions.filter(report_id=report_id)
            if report_id
            else transfer.submissions.all()
        )
        available = []
        for row in rows:
            try:
                check_report(row, transfer, installation)
            except TransferError:
                if report_id:
                    raise
            else:
                available.append(row)
        if report_id and not available:
            raise TransferError("report_unavailable", 404)
        portal.last_seen_at = now
        portal.save(update_fields=("last_seen_at",))
        request.session.set_expiry(1800)
        return transfer, available


def revoke_portal(request):
    transfer, _ = portal_context(request)
    with transaction.atomic():
        lock_installation(transfer.installation_id)
        now = timezone.now()
        if ReportTransfer.objects.filter(pk=transfer.pk, revoked_at__isnull=True).update(
            revoked_at=now
        ):
            record_event(
                action="report_transfer.revoked",
                target_type="report_transfer",
                target_uuid=transfer.pk,
                metadata={"source": "portal", "revoked_at": now.isoformat()},
            )


def logout(request):
    pk = request.session.get("report_portal")
    if pk:
        ReportPortalSession.objects.filter(pk=pk).update(logged_out_at=timezone.now())
    request.session.flush()
