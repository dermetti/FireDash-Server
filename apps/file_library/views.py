import hashlib
import io
import uuid
from datetime import UTC, datetime
from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.scopes import active_department_ids
from apps.file_library import services
from apps.file_library.forms import TemporaryStationAccessForm
from apps.file_library.models import CapacityReview, Content, Entry, Library
from apps.file_library.publication import publication_state, publish_now
from apps.organizations.models import Station
from apps.reference_data.pdf_sandbox import PdfSanitizerError
from apps.reference_data.pdf_validation import PdfValidationError
from apps.reference_data.storage import StorageError


def identifier(value):
    try:
        return uuid.UUID(value) if value else None
    except (ValueError, AttributeError) as error:
        raise services.LibraryError("Invalid item identifier.") from error


@login_required
@require_http_methods(["GET"])
def index(request):
    libraries = services.authorized_libraries(request.user)
    if len(libraries) == 1:
        return redirect("file-library", library_id=libraries[0].id)
    return render(request, "file_library/index.html", {"libraries": libraries})


@login_required
@require_http_methods(["GET"])
def station_hub(request, station_id):
    services.require_scope(request.user, station_id=station_id)
    library = services.get_library(request.user, station_id=station_id)
    return render(request, "file_library/station_hub.html", {"library": library})


@login_required
@require_http_methods(["GET", "POST"])
def library_view(request, library_id):
    library = get_object_or_404(Library, pk=library_id)
    try:
        access = services.require_library_access(request.user, library, permission="read")
    except PermissionDenied:
        if (
            request.method == "GET"
            and library.station_id
            and cast(Station, library.station).department_id in active_department_ids(request.user)
        ):
            department_library = services.get_library(
                request.user, department_id=cast(Station, library.station).department_id
            )
            return redirect("file-library", library_id=department_library.id)
        raise
    current = None
    try:
        current = services.parent_entry(library, identifier(request.GET.get("folder")))
    except services.LibraryError as error:
        raise Http404 from error
    library_name_error = None
    library_name_value = library.name
    if request.method == "POST":
        deletion_access = None
        try:
            action = request.POST.get("action")
            if access == services.ACCESS_TEMPORARY and action != "delete":
                raise PermissionDenied
            common = {"actor": request.user, "library_id": library.id}
            if action == "publish":
                if not library.station_id or access != services.ACCESS_NORMAL:
                    raise PermissionDenied
                publish_now(**common)
                messages.success(
                    request, "Publication requested. Tablet delivery is not confirmed."
                )
            elif action == "upload":
                uploads = request.FILES.getlist("pdfs")
                if not uploads:
                    raise services.LibraryError("Select at least one PDF.")
                for upload in uploads:
                    try:
                        services.upload_pdf(
                            **common, upload=upload, parent_id=current.id if current else None
                        )
                        messages.success(request, f"Uploaded {upload.name}.")
                    except (services.LibraryError, PdfValidationError) as error:
                        messages.error(request, f"{upload.name}: {error}")
                    except (PdfSanitizerError, StorageError, OSError):
                        messages.error(
                            request, f"{upload.name}: PDF processing unavailable; retry later."
                        )
            elif action == "folder":
                services.create_folder(
                    **common,
                    name=request.POST.get("name", ""),
                    parent_id=current.id if current else None,
                )
            elif action == "library_name":
                services.rename_library(**common, name=request.POST.get("name", ""))
            elif action in {"rename", "move", "delete", "replace"}:
                entry_id = identifier(request.POST.get("entry"))
                if not entry_id:
                    raise services.LibraryError("Select an item.")
                if action == "rename":
                    services.edit_entry(
                        **common, entry_id=entry_id, name=request.POST.get("name", "")
                    )
                elif action == "move":
                    services.edit_entry(
                        **common,
                        entry_id=entry_id,
                        move=True,
                        parent_id=identifier(request.POST.get("destination")),
                    )
                elif action == "delete":
                    if request.POST.get("confirm") != "yes":
                        raise services.LibraryError("Confirm recursive deletion explicitly.")
                    deletion_access = services.delete_entry(**common, entry_id=entry_id)
                else:
                    upload = request.FILES.get("pdf")
                    if upload is None:
                        raise services.LibraryError("Select a replacement PDF.")
                    services.upload_pdf(**common, upload=upload, replace_id=entry_id)
            else:
                raise services.LibraryError("Unknown file operation.")
            if action not in {"upload", "publish"}:
                messages.success(
                    request, "Library renamed." if action == "library_name" else "Library updated."
                )
        except (services.LibraryError, PdfValidationError, ValidationError) as error:
            if action == "library_name":
                library_name_error = str(error)
                library_name_value = request.POST.get("name", "")
            else:
                messages.error(request, str(error))
        except (PdfSanitizerError, StorageError, OSError):
            messages.error(request, "PDF processing unavailable; retry later.")
        if action == "delete" and deletion_access == services.ACCESS_TEMPORARY:
            return redirect("file-library-index")
        if library_name_error is None:
            return redirect(request.path if action == "library_name" else request.get_full_path())
    from apps.file_library.capacity import REVIEW_PAGE_SIZE, pending_department_reviews

    department_admin = bool(library.department_id and access == services.ACCESS_NORMAL)
    station_admin = bool(library.station_id and access == services.ACCESS_NORMAL)
    review_page = None
    if department_admin:
        review_page = Paginator(
            pending_department_reviews(library.department), REVIEW_PAGE_SIZE
        ).get_page(request.GET.get("review_page", 1))
    from apps.file_library.capacity import presentation_summary

    review_rows = [
        {"review": review, "summary": presentation_summary(review.projection)}
        for review in review_page or ()
    ]
    return render(
        request,
        "file_library/library.html",
        {
            "library": library,
            "library_name_value": library_name_value,
            "library_name_error": library_name_error,
            "editing_library_name": request.GET.get("rename") == "1"
            or library_name_error is not None,
            "pending_review_page": review_page,
            "pending_review_rows": review_rows,
            "file_count": library.entries.filter(is_folder=False).count(),
            "capacity_reviews": (
                CapacityReview.objects.filter(
                    library=library,
                    consumed_at__isnull=True,
                )
                .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
                .filter(Q(requested_by=request.user) | Q(publication_snapshot__isnull=False))
                .order_by("-created_at", "id")[:10]
                if station_admin
                else ()
            ),
            "publication": publication_state(library) if station_admin else None,
            "access_mode": access,
            "is_department_library_admin": department_admin,
            "is_station_library_admin": station_admin,
            "temporary_access": (
                services.active_temporary_access(request.user)
                if access == services.ACCESS_TEMPORARY or department_admin
                else None
            ),
        },
    )


@login_required
@require_http_methods(["GET"])
def document(request, library_id, entry_id):
    library = get_object_or_404(Library, pk=library_id)
    services.require_library_access(request.user, library, permission="read")
    entry = get_object_or_404(
        Entry.objects.select_related("content"), library=library, pk=entry_id, is_folder=False
    )
    try:
        data = services.content_path(entry.content).read_bytes()
    except (OSError, services.LibraryError) as error:
        raise Http404("PDF unavailable") from error
    if (
        len(data) != cast(Content, entry.content).size_bytes
        or hashlib.sha256(data).hexdigest() != cast(Content, entry.content).sha256
    ):
        raise Http404("PDF integrity verification failed")
    response = FileResponse(
        io.BytesIO(data),
        content_type="application/pdf",
        as_attachment=request.GET.get("download") == "1",
        filename=entry.name,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@login_required
@require_http_methods(["POST"])
def activate_station_access(request, station_id):
    station = get_object_or_404(Station.objects.select_related("department"), pk=station_id)
    department_library = services.get_library(request.user, department_id=station.department_id)
    if request.POST.get("confirm") != "yes":
        return HttpResponseBadRequest("Confirm temporary station access explicitly.")
    require_recent_reauthentication(
        request, return_url=reverse("file-library", args=(department_library.id,))
    )
    access = services.activate_temporary_access(actor=request.user, station=station)
    library = Library.objects.get(station=access.station)
    messages.success(
        request,
        f"Temporary view-and-delete access activated for {station.name} until "
        f"{access.expires_at:%Y-%m-%d %H:%M %Z}.",
    )
    return redirect("file-library", library_id=library.id)


@login_required
@require_http_methods(["POST"])
def select_station_access(request):
    try:
        department_id = identifier(request.POST.get("department_id"))
    except services.LibraryError:
        return HttpResponseBadRequest("Invalid department identifier.")
    services.require_scope(request.user, department_id=department_id)
    library = services.get_library(request.user, department_id=department_id)
    form = TemporaryStationAccessForm(
        request.POST, department_id=department_id, query=request.POST.get("station_q", "")[:255]
    )
    if not form.is_valid():
        from apps.file_library.explorer_views import station_access_response

        return station_access_response(request, library, form)
    require_recent_reauthentication(
        request,
        return_url=reverse("file-library-station-access-choose", args=(library.id,))
        + f"?station_id={form.cleaned_data['station_id'].id}",
    )
    station = form.cleaned_data["station_id"]
    access = services.activate_temporary_access(actor=request.user, station=station)
    station_library = Library.objects.get(station=access.station)
    messages.success(request, "Temporary station access activated.")
    from apps.file_library.explorer_views import finish

    return finish(request, reverse("file-library", args=(station_library.id,)))


@login_required
@require_http_methods(["POST"])
def release_station_access(request):
    access = services.revoke_temporary_access(actor=request.user, reason="released")
    if access:
        messages.success(request, "Temporary station access released.")
    return redirect("file-library-index")


@login_required
@require_http_methods(["GET", "POST"])
def capacity_review(request, review_id):
    from apps.file_library.capacity import (
        decide,
        department_for,
        presentation_summary,
        review_management_url,
    )

    review = get_object_or_404(
        CapacityReview.objects.select_related(
            "library__department", "library__station__department"
        ),
        pk=review_id,
    )
    library = review.library
    department = department_for(library)
    department_admin = department.id in active_department_ids(request.user)
    can_view = False
    if request.user.id == review.requested_by_id or review.publication_snapshot is not None:
        try:
            services.require_library(request.user, library)
            can_view = True
        except PermissionDenied:
            pass
    if not department_admin and not can_view:
        raise PermissionDenied
    temporary = services.active_temporary_access(request.user)
    management_url = (
        review_management_url(department, review)
        if department_admin
        else reverse("file-library", args=(library.id,))
    )
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"approve", "reject"}:
                if not department_admin or temporary:
                    raise PermissionDenied
                require_recent_reauthentication(
                    request, return_url=reverse("file-library-capacity-review", args=(review.id,))
                )
                decision = decide(
                    actor=request.user,
                    review_id=review.id,
                    confirm=request.POST.get("confirm") == "yes",
                    reauthenticated_at=datetime.fromtimestamp(
                        request.session["recent_reauthentication_at"], tz=UTC
                    ),
                    approve=action == "approve",
                    expected_fingerprint=request.POST.get("fingerprint", ""),
                )
                if decision.superseded_at:
                    messages.warning(
                        request, "This assessment was superseded. Review the current candidate."
                    )
                else:
                    messages.success(request, "Publication capacity decision recorded.")
            else:
                return HttpResponseBadRequest("Unknown review operation.")
        except (services.LibraryError, PdfValidationError, ValidationError) as error:
            messages.error(request, str(error))
        except (PdfSanitizerError, StorageError, OSError):
            messages.error(request, "PDF processing unavailable; retry later.")
        return redirect("file-library-capacity-review", review_id=review.id)
    return render(
        request,
        "file_library/capacity_review.html",
        {
            "review": review,
            "review_management_url": management_url,
            "capacity_summary": presentation_summary(review.projection),
            "library": library,
            "department": department,
            "department_admin": department_admin,
            "can_decide": department_admin and not temporary,
            "can_view": can_view,
            "pending": review.decision == "pending"
            and not review.superseded_at
            and review.publication_snapshot is not None,
            "approved": review.decision == "approved"
            and not review.consumed_at
            and not review.superseded_at,
            "temporary": temporary,
        },
    )
