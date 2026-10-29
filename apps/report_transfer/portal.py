"""Small German Django portal; no general account authentication or public files."""

import io
import logging
from copy import deepcopy
from datetime import datetime
from functools import wraps
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.db import transaction
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST
from PIL import Image, ImageOps

from apps.audit.services import record_event
from apps.report_transfer import storage, transfers
from apps.report_transfer.contract import TransferError, decode
from apps.report_transfer.exports import load_portal_submission
from apps.report_transfer.models import ReportPortalSession
from apps.report_transfer.pdf import render_pdf
from apps.report_transfer.services import effective_expiry, record_successful_download

logger = logging.getLogger(__name__)


def boundary(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except (TransferError, OSError) as error:
            status = error.status if isinstance(error, TransferError) else 503
            return render(
                request,
                "report_transfer/message.html",
                {
                    "message": "Dieser Zugang oder Bericht ist nicht verfügbar. "
                    "Fordern Sie bei Bedarf einen neuen Link in der Tablet-App an."
                },
                status=status,
            )

    return wrapped


@require_GET
def access(request, token):
    try:
        row = transfers.token_transfer(token)
        state = "used" if row.consumed_at else "fresh"
    except TransferError:
        state = "expired"
    return render(
        request,
        "report_transfer/access.html",
        {
            "token_state": state,
            "redeem_url": reverse("report-portal-redeem", args=[token]),
            "revoke_url": reverse("report-portal-token-revoke", args=[token]),
        },
    )


@require_POST
@boundary
def redeem(request, token):
    transfers.redeem(request, token)
    # Session CSRF secret must also change at the authentication boundary.
    from django.middleware.csrf import rotate_token

    rotate_token(request)
    return redirect("report-portal-list")


@require_POST
@boundary
def revoke_token(request, token):
    row = transfers.token_transfer(token)
    if row.consumed_at is None:
        raise TransferError("access_unavailable", 403)
    if request.POST.get("confirm") != "yes":
        return render(
            request,
            "report_transfer/confirm_revoke.html",
            {"action_url": reverse("report-portal-token-revoke", args=[token])},
        )
    transfers.revoke_token(token)
    return redirect("report-portal-ended")


@require_GET
def ended(request):
    return render(
        request,
        "report_transfer/message.html",
        {
            "message": "Der Zugang wurde beendet. "
            "Sie können in der Tablet-App einen neuen Link anfordern."
        },
    )


@require_GET
@boundary
def report_list(request):
    _, rows = transfers.portal_context(request)
    reports = []
    for row in rows:
        current, document, _ = load_portal_submission(request, row.report_id)
        reports.append(
            {
                "title": document["title"],
                "report_type": document["report_type"],
                "received_at": current.created_at,
                "header_fields": display_fields(document["header"]),
                "finalized_at": datetime.fromisoformat(
                    document["finalization"]["finalized_at"].replace("Z", "+00:00")
                ),
                "photo_count": len(document["attachments"]),
                "report_id": str(row.report_id),
                "expires_at": effective_expiry(current),
                "detail_url": reverse("report-portal-detail", args=[row.report_id]),
            }
        )
    reports.sort(key=lambda item: (item["finalized_at"], item["report_id"]))
    return render(
        request, "report_transfer/list.html", {"reports": reports, "portal_authenticated": True}
    )


def display_value(typed):
    if typed["type"] == "boolean":
        return "Ja" if typed["value"] else "Nein"
    return str(typed["value"])


def display_fields(fields):
    return [
        {"label": field["label"], "display_value": display_value(field["value"])}
        for field in fields
    ]


@require_GET
@boundary
def detail(request, report_id):
    row, document, _ = load_portal_submission(request, report_id)
    sections = deepcopy(document["sections"])
    photos = []
    for photo in document["attachments"]:
        photos.append(
            {
                **photo,
                "preview_url": reverse("report-portal-preview", args=[report_id, photo["id"]]),
                "download_url": reverse("report-portal-jpeg", args=[report_id, photo["id"]]),
            }
        )
    photo_by_id = {p["id"]: p for p in photos}
    for section in sections:
        for block in section["blocks"]:
            if block["kind"] == "fields":
                block["display_fields"] = display_fields(block["fields"])
            elif block["kind"] == "table":
                block["display_rows"] = [
                    [display_value(cell) for cell in cells] for cells in block["rows"]
                ]
            elif block["kind"] == "attachment_refs":
                block["photos"] = [photo_by_id[i] for i in block["attachment_ids"]]
    return render(
        request,
        "report_transfer/detail.html",
        {
            "portal_authenticated": True,
            "document": document,
            "header_fields": display_fields(document["header"]),
            "sections": sections,
            "photos": photos,
            "pdf_url": reverse("report-portal-pdf", args=[report_id]),
            "expires_at": effective_expiry(row),
            "received_at": row.created_at,
            "document_digest": row.document_digest,
        },
    )


def complete_download(request, row, file_type, *, transfer_id):
    receipt = None
    completed_at = timezone.now()
    # Created only on normal stream exhaustion. UUID deduplicates a recovered
    # receipt after a crash between database commit and receipt removal.
    download_audit = {
        "download_id": str(uuid4()),
        "transfer_id": str(transfer_id),
        "file_type": file_type,
    }
    try:
        receipt = storage.write_download_receipt(
            row.pk, completed_at, download_audit=download_audit
        )
    except (OSError, TransferError):
        logger.warning("Report download receipt could not be journaled.")
    try:
        # Keep completion and generation checks serialized with revocation.
        with transaction.atomic():
            transfers.portal_context(request, report_id=row.report_id)
            record_successful_download(
                submission_pk=row.pk, completed_at=completed_at, download_audit=download_audit
            )
        if receipt:
            receipt.unlink(missing_ok=True)
    except Exception:
        # Once journaled, status/access enforce its ceiling even before the
        # scheduled cleanup reconciles the receipt into the database.
        logger.warning("Report download completion could not be recorded.")


def download_response(request, row, payload, media_type, extension):
    """Count only normal stream exhaustion, after each yielded chunk was sent.

    WSGI/ASGI can establish server handoff, never browser receipt/save. Closing
    a partially consumed generator cannot execute the completion hook.
    """
    if (
        "prefetch"
        in (request.headers.get("Sec-Purpose", "") + request.headers.get("Purpose", "")).lower()
    ):
        raise TransferError("download_requires_explicit_request", 400)
    transfer_id = ReportPortalSession.objects.values_list("transfer_id", flat=True).get(
        pk=request.session["report_portal"]
    )

    def chunks():
        for offset in range(0, len(payload), 256 * 1024):
            transfers.portal_context(request, report_id=row.report_id)
            yield payload[offset : offset + 256 * 1024]
        complete_download(
            request, row, "pdf" if extension == "pdf" else "jpeg", transfer_id=transfer_id
        )

    async def async_chunks():
        for offset in range(0, len(payload), 256 * 1024):
            await sync_to_async(transfers.portal_context)(request, report_id=row.report_id)
            yield payload[offset : offset + 256 * 1024]
        await sync_to_async(complete_download)(
            request, row, "pdf" if extension == "pdf" else "jpeg", transfer_id=transfer_id
        )

    response = StreamingHttpResponse(
        async_chunks() if hasattr(request, "scope") else chunks(), content_type=media_type
    )
    response["Content-Disposition"] = f'attachment; filename="report-{row.report_id}.{extension}"'
    response["Content-Length"] = str(len(payload))
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
@boundary
@sensitive_variables()
def pdf(request, report_id):
    row, document, attachments = load_portal_submission(request, report_id)
    payload = render_pdf(
        document,
        attachment_hashes={a["id"]: a["sha256"] for a in attachments},
        received_at=row.created_at,
    )
    transfer, _ = transfers.portal_context(request, report_id=report_id)
    record_event(
        action="report_transfer.pdf_generated",
        department=row.department,
        target_type="report_submission",
        target_uuid=row.submission_id,
        metadata={
            "file_type": "pdf",
            "transfer_id": str(transfer.pk),
            "installation_id": str(row.installation_id),
        },
    )
    return download_response(request, row, payload, "application/pdf", "pdf")


@require_GET
@boundary
@sensitive_variables()
def jpeg(request, report_id, attachment_id, *, preview=False):
    row, _, attachments = load_portal_submission(request, report_id)
    attachment = next((a for a in attachments if a["id"] == str(attachment_id)), None)
    if attachment is None:
        raise TransferError("attachment_not_found", 404)
    payload = decode(attachment["data"])
    if preview:
        with Image.open(io.BytesIO(payload)) as image:
            # EXIF orientation/metadata never leaves the server in thumbnails.
            image = ImageOps.exif_transpose(image)
            image.thumbnail((640, 480))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=75)
        transfers.portal_context(request, report_id=report_id)
        return HttpResponse(output.getvalue(), content_type="image/jpeg")
    return download_response(request, row, payload, "image/jpeg", "jpg")


@require_POST
def logout(request):
    transfers.logout(request)
    return redirect("report-portal-ended")


@require_POST
@boundary
def revoke(request):
    transfers.portal_context(request)
    if request.POST.get("confirm") != "yes":
        return render(
            request,
            "report_transfer/confirm_revoke.html",
            {"action_url": reverse("report-portal-revoke"), "portal_authenticated": True},
        )
    transfers.revoke_portal(request)
    transfers.logout(request)
    return redirect("report-portal-ended")
