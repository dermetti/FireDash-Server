import hashlib
import io
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.file_library import services
from apps.file_library.models import Entry, Library
from apps.file_library.publication import publication_state, publish_now
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
    services.require_library(request.user, library)
    current = None
    try:
        current = services.parent_entry(library, identifier(request.GET.get("folder")))
    except services.LibraryError as error:
        raise Http404 from error
    if request.method == "POST":
        try:
            action = request.POST.get("action")
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
                    services.delete_entry(**common, entry_id=entry_id)
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
        },
    )


@login_required
@require_http_methods(["GET"])
def document(request, library_id, entry_id):
    library = get_object_or_404(Library, pk=library_id)
    services.require_library(request.user, library)
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
