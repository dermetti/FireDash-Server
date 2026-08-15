from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.publications.registry import get_dataset_definition
from apps.publications.services import (
    PublicationError,
    bulk_request_rebuilds,
    publish_publication,
    reject_publication,
    request_rebuild,
    rollback_publication,
)
from apps.publications.state import (
    BUILDING,
    FAILED,
    QUEUED,
    UPDATE_QUEUED,
    operational_summary,
    scope_operational_states,
)

HISTORY_PAGE_SIZE = 25


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    return department


def _scope_or_403(request: HttpRequest, scope_id) -> DatasetScopeState:
    scope = get_object_or_404(
        DatasetScopeState.objects.select_related("department", "station"), pk=scope_id
    )
    require_department_admin(request.user, scope.department)
    return scope


@login_required
@require_http_methods(["GET"])
def publications(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    rows = scope_operational_states(department)
    raw = operational_summary(rows)
    summary = {
        "total": raw["total"],
        "current": raw["CURRENT"],
        "queued": raw["UPDATE_QUEUED"] + raw["QUEUED"],
        "building": raw["BUILDING"],
        "needs_attention": raw["FAILED"] + raw["NEEDS_REBUILD"],
        "ready_to_publish": raw["READY_TO_PUBLISH"],
    }

    building = [row for row in rows if row["state"] == BUILDING]
    queued = [row for row in rows if row["state"] in (UPDATE_QUEUED, QUEUED)]
    failed = [row for row in rows if row["state"] == FAILED]

    history = (
        DatasetPublication.objects.filter(department=department)
        .select_related("station", "created_by", "published_by")
        .order_by("-created_at")
    )
    paginator = Paginator(history, HISTORY_PAGE_SIZE)
    page_number = request.GET.get("page")
    history_page = paginator.get_page(page_number)

    return render(
        request,
        "publications/list.html",
        {
            "department": department,
            "rows": rows,
            "summary": summary,
            "building": building,
            "queued": queued,
            "failed": failed,
            "history_page": history_page,
        },
    )


@login_required
@require_http_methods(["POST"])
def scope_rebuild(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    require_recent_reauthentication(
        request,
        return_url=reverse("publications-list", args=(scope.department_id,)),
    )
    try:
        request_rebuild(
            actor=request.user,
            department=scope.department,
            station=scope.station,
            dataset_type_code=scope.dataset_type_code,
        )
    except PublicationError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Publication rebuild requested.")
    return redirect("publications-list", department_id=scope.department_id)


@login_required
@require_http_methods(["POST"])
def bulk_rebuild(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    require_recent_reauthentication(
        request,
        return_url=reverse("publications-list", args=(department.id,)),
    )
    result = bulk_request_rebuilds(actor=request.user, department=department)
    requested = result["requested"]
    already_queued = result["already_queued"]
    already_current = result["already_current"]
    if requested:
        messages.success(
            request,
            f"{requested} dataset rebuild{'s' if requested != 1 else ''} requested. "
            f"{already_queued} were already queued, {already_current} were already current.",
        )
    else:
        messages.info(
            request,
            "All publication datasets are current. No rebuilds were requested.",
        )
    return redirect("publications-list", department_id=department.id)


@login_required
@require_http_methods(["GET", "POST"])
def publication_review(request: HttpRequest, publication_id) -> HttpResponse:
    publication = get_object_or_404(
        DatasetPublication.objects.select_related("department", "station", "supersedes"),
        pk=publication_id,
        department_id__in=active_department_ids(request.user),
    )
    require_department_admin(request.user, publication.department)
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=reverse("publications-review", args=(publication.id,)),
        )
        action = request.POST.get("action")
        try:
            if action == "publish":
                publish_publication(actor=request.user, publication=publication)
                message = "Publication published."
            elif action == "reject":
                reject_publication(actor=request.user, publication=publication)
                message = "Publication rejected."
            elif action == "rollback":
                rollback_publication(actor=request.user, publication=publication)
                message = "Publication restored."
            else:
                raise PermissionDenied("Unknown publication review action.")
        except PublicationError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, message)
            return redirect("publications-list", department_id=publication.department_id)
    return render(
        request,
        "publications/review.html",
        {
            "publication": publication,
            "dataset_name": get_dataset_definition(publication.dataset_type_code).display_name,
            "scope_label": publication.station.name if publication.station else "Department",
        },
    )
