"""Shared explorer and focused, progressively enhanced file dialogs."""

from typing import cast
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import InvalidPage, Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from apps.file_library import services
from apps.file_library.explorer import Sort, read_explorer
from apps.file_library.forms import ExplorerActionForm, TemporaryStationAccessForm
from apps.file_library.models import Entry, Library
from apps.file_library.views import identifier
from apps.organizations.models import Station
from apps.reference_data.pdf_sandbox import PdfSanitizerError
from apps.reference_data.pdf_validation import PdfValidationError
from apps.reference_data.storage import StorageError


def finish(request, url):
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": url})
    return redirect(url)


@login_required
@require_GET
def explorer(request, library_id):
    try:
        direction = request.GET.get("direction", "asc")
        if direction not in {"asc", "desc"}:
            return HttpResponseBadRequest("Invalid sort direction.")
        model = read_explorer(
            actor=request.user,
            library_id=library_id,
            folder_id=identifier(request.GET.get("folder")),
            query=request.GET.get("q", ""),
            sort=cast(Sort, request.GET.get("sort", "name")),
            descending=direction == "desc",
            page=int(request.GET.get("page", "1")),
            page_size=int(request.GET.get("page_size", "25")),
        )
    except (Library.DoesNotExist, services.LibraryError, InvalidPage, ValueError) as error:
        raise Http404("The requested folder or page is unavailable.") from error
    query = request.GET.copy()
    query.pop("page", None)
    previous = query.copy()
    previous["page"] = str(model.page.number - 1)
    following = query.copy()
    following["page"] = str(model.page.number + 1)
    context = {
        "library": model.library,
        "access_mode": model.access_mode,
        "temporary_access": services.active_temporary_access(request.user)
        if model.access_mode == services.ACCESS_TEMPORARY
        else None,
        "current_folder": model.current_folder,
        "breadcrumbs": model.breadcrumbs,
        "items": model.items,
        "page": model.page,
        "query": model.query,
        "sort": model.sort,
        "direction": direction,
        "can_manage_files": model.access_mode == services.ACCESS_NORMAL,
        "previous_querystring": previous.urlencode(),
        "next_querystring": following.urlencode(),
    }
    return render(
        request,
        "file_library/_explorer_listing.html"
        if request.headers.get("HX-Request")
        and not request.headers.get("HX-History-Restore-Request")
        else "file_library/explorer.html",
        context,
    )


ACTION_LABELS = {
    "upload": ("Upload PDFs", "Upload PDFs"),
    "new-folder": ("New folder", "Create folder"),
    "rename": ("Rename", "Save name"),
    "move": ("Move", "Move here"),
    "replace": ("Replace PDF", "Replace PDF"),
    "delete": ("Delete", "Delete"),
}


@login_required
@require_http_methods(["GET", "POST"])
def action(request, library_id, action):
    if action not in ACTION_LABELS:
        raise Http404
    library = get_object_or_404(Library, pk=library_id)
    access = services.require_library_access(request.user, library, permission="read")
    if action != "delete":
        services.require_library(request.user, library)
    data = request.POST if request.method == "POST" else request.GET
    try:
        current = services.parent_entry(library, identifier(data.get("folder")))
        breadcrumbs = services.folder_ancestry(library, current)
        entry = None
        if action not in {"upload", "new-folder"}:
            entry_id = identifier(data.get("entry"))
            if not entry_id:
                raise services.LibraryError("Select an item.")
            entry = services.scoped_entry(library, entry_id)
            if action == "replace" and entry.is_folder:
                raise services.LibraryError("Select a PDF document.")
        destination = services.parent_entry(library, identifier(data.get("destination")))
        destination_path = services.folder_ancestry(library, destination)
    except services.LibraryError as error:
        raise Http404 from error
    form = ExplorerActionForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        action=action,
        initial={"name": entry.name if entry else ""},
    )
    review_links = []
    if request.method == "POST" and form.is_valid():
        common = {"actor": request.user, "library_id": library.id}
        try:
            deletion_access = None
            if action in {"upload", "replace"}:
                uploads = (
                    form.cleaned_data["pdfs"] if action == "upload" else [form.cleaned_data["pdf"]]
                )
                for upload in uploads:
                    try:
                        services.upload_pdf(
                            **common,
                            upload=upload,
                            parent_id=current.id if current else None,
                            replace_id=entry.id if action == "replace" and entry else None,
                        )
                        messages.success(request, f"Accepted {upload.name}.")
                    except services.CapacityWarning as error:
                        form.add_error(None, f"{upload.name}: {error}")
                        review_links.append(error.review)
                    except (services.LibraryError, PdfValidationError, ValidationError) as error:
                        form.add_error(None, f"{upload.name}: {error}")
                    except (PdfSanitizerError, StorageError, OSError):
                        form.add_error(
                            None, f"{upload.name}: PDF processing unavailable; retry later."
                        )
            elif action == "new-folder":
                services.create_folder(
                    **common,
                    name=form.cleaned_data["name"],
                    parent_id=current.id if current else None,
                )
            elif action == "rename":
                assert entry is not None
                services.edit_entry(**common, entry_id=entry.id, name=form.cleaned_data["name"])
            elif action == "move":
                assert entry is not None
                services.edit_entry(
                    **common,
                    entry_id=entry.id,
                    move=True,
                    parent_id=destination.id if destination else None,
                )
            elif action == "delete":
                assert entry is not None
                deletion_access = services.delete_entry(**common, entry_id=entry.id)
            if not form.errors:
                if action not in {"upload", "replace"}:
                    messages.success(request, "Library updated.")
                url = reverse("file-library-explorer", args=(library.id,))
                if current and Entry.objects.filter(pk=current.id).exists():
                    url += "?" + urlencode({"folder": str(current.id)})
                if deletion_access == services.ACCESS_TEMPORARY:
                    url = reverse("file-library-index")
                return finish(request, url)
        except (services.LibraryError, ValidationError) as error:
            form.add_error("name" if action in {"rename", "new-folder"} else None, str(error))
    destinations = None
    if action == "move":
        assert entry is not None
        try:
            destinations = Paginator(
                library.entries.filter(is_folder=True, parent=destination)
                .exclude(pk=entry.pk)
                .order_by("name", "id"),
                25,
            ).page(request.GET.get("page", 1))
        except InvalidPage as error:
            raise Http404 from error
    for name in form.errors:
        if name in form.fields:
            form.fields[name].widget.attrs.update(
                {"aria-invalid": "true", "aria-describedby": f"id_{name}_error"}
            )
    context = {
        "library": library,
        "access_mode": access,
        "entry": entry,
        "current_folder": current,
        "folder_display_path": " / ".join(["Files", *(folder.name for folder in breadcrumbs)]),
        "destination": destination,
        "destination_path": destination_path,
        "destinations": destinations,
        "form": form,
        "action": action,
        "modal_title": ACTION_LABELS[action][0],
        "is_modal": bool(request.headers.get("HX-Request")),
        "submit_label": ACTION_LABELS[action][1],
        "action_url": request.get_full_path(),
        "multipart": action in {"upload", "replace"},
        "review_links": review_links,
        "max_file_bytes": settings.FILE_LIBRARY_MAX_FILE_BYTES,
        "back_url": reverse("file-library-explorer", args=(library.id,))
        + ("?" + urlencode({"folder": str(current.id)}) if current else ""),
    }
    return render(
        request,
        "file_library/_action_dialog.html"
        if request.headers.get("HX-Request")
        else "file_library/action.html",
        context,
    )


def station_access_response(request, library, form):
    services.require_library(request.user, library)
    if not library.department_id:
        raise PermissionDenied
    selected = form.data.get("station_id") if form.is_bound else form.initial.get("station_id", "")
    selected_label = next((label for value, label in form.options if value == str(selected)), "")
    context = {
        "library": library,
        "station_access_form": form,
        "is_modal": bool(request.headers.get("HX-Request")),
        "station_query": request.GET.get("station_q", request.POST.get("station_q", ""))[:255],
        "selected_station": selected if selected_label else "",
        "selected_label": selected_label,
    }
    return render(
        request,
        "file_library/_station_access_dialog.html"
        if request.headers.get("HX-Request")
        else "file_library/station_access.html",
        context,
    )


@login_required
@require_GET
def choose_station_access(request, library_id):
    library = get_object_or_404(Library, pk=library_id)
    services.require_library(request.user, library)
    if not library.department_id:
        raise PermissionDenied
    form = TemporaryStationAccessForm(
        department_id=library.department_id,
        query=request.GET.get("station_q", "").strip()[:255],
        initial={"station_id": request.GET.get("station_id", "")},
    )
    return station_access_response(request, library, form)


@login_required
@require_GET
def station_search(request, department_id):
    services.require_scope(request.user, department_id=department_id)
    query = request.GET.get("q", "").strip()[:255]
    results = Station.objects.filter(department_id=department_id, active=True)
    if query:
        results = results.filter(Q(name__icontains=query) | Q(short_code__icontains=query))
    return render(
        request,
        "file_library/_station_search_results.html",
        {"results": results.order_by("name", "id")[:20], "search_query": query},
    )
