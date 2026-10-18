import hashlib
import io
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.scopes import active_department_ids
from apps.file_library import services
from apps.file_library.models import Entry, Library
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
            and library.station.department_id in active_department_ids(request.user)
        ):
            department_library = services.get_library(
                request.user, department_id=library.station.department_id
            )
            return redirect("file-library", library_id=department_library.id)
        raise
    current = None
    try:
        current = services.parent_entry(library, identifier(request.GET.get("folder")))
    except services.LibraryError as error:
        raise Http404 from error
    if request.method == "POST":
        deletion_access = None
        try:
            action = request.POST.get("action")
            if access == services.ACCESS_TEMPORARY and action != "delete":
                raise PermissionDenied
            common = {"actor": request.user, "library_id": library.id}
            if action == "publish":
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
                messages.success(request, "Library updated.")
        except (services.LibraryError, PdfValidationError, ValidationError) as error:
            messages.error(request, str(error))
        except (PdfSanitizerError, StorageError, OSError):
            messages.error(request, "PDF processing unavailable; retry later.")
        if action == "delete" and deletion_access == services.ACCESS_TEMPORARY:
            return redirect("file-library-index")
        return redirect(request.get_full_path())
    query = request.GET.get("q", "").strip()[:255]
    entries = library.entries.select_related("content", "modified_by", "parent")
    entries = entries.filter(name__icontains=query) if query else entries.filter(parent=current)
    breadcrumbs = []
    cursor = current
    while cursor:
        breadcrumbs.insert(0, cursor)
        cursor = cursor.parent
    folders = list(library.entries.filter(is_folder=True).select_related("parent"))
    by_id = {folder.id: folder for folder in folders}
    for folder in folders:
        names, cursor = [folder.name], folder
        while cursor.parent_id:
            cursor = by_id[cursor.parent_id]
            names.insert(0, cursor.name)
        folder.display_path = " / ".join(names)
    return render(
        request,
        "file_library/library.html",
        {
            "library": library,
            "entries": entries,
            "current": current,
            "breadcrumbs": breadcrumbs,
            "folders": sorted(folders, key=lambda folder: folder.display_path),
            "query": query,
            "max_file_bytes": settings.FILE_LIBRARY_MAX_FILE_BYTES,
            "publication": publication_state(library),
            "access_mode": access,
            "temporary_access": (
                services.active_temporary_access(request.user)
                if access == services.ACCESS_TEMPORARY or library.department_id
                else None
            ),
            "available_stations": (
                Station.objects.filter(
                    department_id=library.department_id, active=True
                ).order_by("name", "id")
                if library.department_id
                else ()
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
        len(data) != entry.content.size_bytes
        or hashlib.sha256(data).hexdigest() != entry.content.sha256
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
    department_library = services.get_library(
        request.user, department_id=station.department_id
    )
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
def release_station_access(request):
    access = services.revoke_temporary_access(actor=request.user, reason="released")
    if access:
        messages.success(request, "Temporary station access released.")
    return redirect("file-library-index")
