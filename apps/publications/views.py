from __future__ import annotations

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
    cancel_publication_build,
    delete_publication,
    delete_staged_publication,
    request_rebuild,
    rollback_publication,
)
from apps.publications.state import (
    BUILDING,
    FAILED,
    NEEDS_REBUILD,
    NOT_PUBLISHED,
    QUEUED,
    READY_TO_PUBLISH,
    UPDATE_QUEUED,
    scope_operational_states,
)

SCOPE_PAGE_SIZE = 50
HISTORY_PAGE_SIZE = 25
SCOPE_FILTERS = {
    "": "All",
    "current": "Current",
    "scheduled": "Update scheduled",
    "building": "Building",
    "failed": "Failed",
    "not_published": "Not published",
}


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


def _eligible_predecessor_exists(scope_id, *, before_version: int) -> bool:
    return DatasetPublication.objects.filter(
        scope_state_id=scope_id,
        status=DatasetPublication.Status.SUPERSEDED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path__gt="",
        version_number__lt=before_version,
    ).exists()


def _row_actions(row: dict[str, object]) -> dict[str, bool]:
    """UI affordances only; Phase 4A services remain authoritative."""
    state = row["state"]
    has_predecessor = bool(row["has_eligible_predecessor"])
    return {
        "delete_staged": state in (UPDATE_QUEUED, QUEUED)
        and (row["active_job_publication_id"] or row["latest_publication_id"]) is not None,
        "cancel_build": state == BUILDING
        and (row["active_job_publication_id"] or row["latest_publication_id"]) is not None,
        "build_update": state in (FAILED, NEEDS_REBUILD, NOT_PUBLISHED, READY_TO_PUBLISH),
        "rollback": has_predecessor and state not in (UPDATE_QUEUED, QUEUED, BUILDING),
    }


def _decorate_scope_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    scope_ids = [row["scope_id"] for row in rows]
    predecessor_versions: dict[object, list[int]] = {}
    if scope_ids:
        for scope_id, version_number in DatasetPublication.objects.filter(
            scope_state_id__in=scope_ids,
            status=DatasetPublication.Status.SUPERSEDED,
            artifact_ready=True,
            artifact_status=DatasetPublication.ArtifactStatus.READY,
            artifact_path__gt="",
        ).values_list("scope_state_id", "version_number"):
            predecessor_versions.setdefault(scope_id, []).append(version_number)
    for row in rows:
        current_version = row["distributed_version"]
        row["has_eligible_predecessor"] = bool(
            current_version is not None
            and any(
                version_number < current_version
                for version_number in predecessor_versions.get(row["scope_id"], [])
            )
        )
        row["actions"] = _row_actions(row)
        row["update_publication_id"] = (
            row["active_job_publication_id"] or row["latest_publication_id"]
        )
        row["update_version"] = (
            row["active_job_publication_version"] or row["latest_publication_version"]
        )
        if row["state"] not in (UPDATE_QUEUED, QUEUED, BUILDING, FAILED):
            row["update_publication_id"] = None
            row["update_version"] = None
        row["last_changed"] = row["last_activity"]
    return rows


def _matches_filter(row: dict[str, object], selected_filter: str) -> bool:
    state = row["state"]
    return {
        "": True,
        "current": state == "CURRENT",
        "scheduled": state in (UPDATE_QUEUED, QUEUED),
        "building": state == BUILDING,
        "failed": state == FAILED,
        "not_published": state == NOT_PUBLISHED,
    }.get(selected_filter, True)


def _publication_list_context(request: HttpRequest, department: Department) -> dict[str, object]:
    selected_filter = request.GET.get("state", "")
    if selected_filter not in SCOPE_FILTERS:
        selected_filter = ""
    rows = _decorate_scope_rows(scope_operational_states(department))
    rows = [row for row in rows if _matches_filter(row, selected_filter)]
    paginator = Paginator(rows, SCOPE_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    page_query = request.GET.copy()
    page_query.pop("page", None)
    return {
        "department": department,
        "scope_rows": page.object_list,
        "page": page,
        "total_count": paginator.count,
        "selected_filter": selected_filter,
        "scope_filters": SCOPE_FILTERS,
        "page_query": page_query.urlencode(),
    }


def _scope_row_context(scope: DatasetScopeState) -> dict[str, object]:
    rows = _decorate_scope_rows(scope_operational_states(scope.department, scope_ids={scope.id}))
    if not rows:
        raise PermissionDenied("Dataset scope is unavailable.")
    return {"row": rows[0], "department": scope.department}


def _scope_title(scope: DatasetScopeState) -> str:
    definition = get_dataset_definition(scope.dataset_type_code)
    return (
        f"{definition.display_name} · {scope.station.short_code}"
        if scope.station
        else definition.display_name
    )


def _scope_history_context(scope: DatasetScopeState, page_number) -> dict[str, object]:
    publications = (
        DatasetPublication.objects.filter(scope_state=scope)
        .select_related("created_by", "published_by")
        .order_by("-version_number")
    )
    page = Paginator(publications, HISTORY_PAGE_SIZE).get_page(page_number)
    current_id = scope.current_published_publication_id
    has_live_attempt = DatasetPublication.objects.filter(
        scope_state=scope,
        status__in=(DatasetPublication.Status.STAGED, DatasetPublication.Status.BUILDING),
    ).exists()
    for publication in page.object_list:
        publication.can_rollback = (
            publication.status == DatasetPublication.Status.SUPERSEDED
            and publication.artifact_ready
            and publication.artifact_status == DatasetPublication.ArtifactStatus.READY
            and bool(publication.artifact_path)
            and not has_live_attempt
        )
        publication.can_delete = publication.status == DatasetPublication.Status.SUPERSEDED or (
            publication.id == current_id
            and _eligible_predecessor_exists(scope.id, before_version=publication.version_number)
            and not has_live_attempt
        )
        publication.can_delete_active = publication.id == current_id and publication.can_delete
        publication.can_delete_historical = (
            publication.status == DatasetPublication.Status.SUPERSEDED
        )
    return {"history_page": page, "has_live_attempt": has_live_attempt}


def _modal_redirect(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
@require_http_methods(["GET"])
def publications(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    context = _publication_list_context(request, department)
    if request.headers.get("HX-Request") == "true":
        return render(request, "publications/_scope_results.html", context)
    return render(request, "publications/list.html", context)


@login_required
@require_http_methods(["GET"])
def publication_status(request: HttpRequest, department_id) -> HttpResponse:
    """Compatibility polling endpoint; scope-row polling is preferred in Phase 4B."""
    department = _department_or_403(request, department_id)
    return render(
        request, "publications/_scope_results.html", _publication_list_context(request, department)
    )


@login_required
@require_http_methods(["GET"])
def publication_scope_row(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    return render(request, "publications/_scope_row.html", _scope_row_context(scope))


@login_required
@require_http_methods(["GET"])
def publication_scope_status(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    return render(
        request,
        "publications/_scope_detail_status.html",
        _scope_row_context(scope) | {"scope": scope},
    )


@login_required
@require_http_methods(["GET"])
def publication_scope_detail(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    context = _scope_row_context(scope) | _scope_history_context(scope, request.GET.get("page"))
    context.update({"scope": scope, "scope_title": _scope_title(scope)})
    return render(request, "publications/scope_detail.html", context)


@login_required
@require_http_methods(["POST"])
def scope_rebuild(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    return_url = reverse("publications-scope-detail", args=(scope.id,))
    require_recent_reauthentication(request, return_url=return_url)
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
    return redirect("publications-scope-detail", scope_id=scope.id)


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
        messages.info(request, "All publication datasets are current. No rebuilds were requested.")
    return redirect("publications-list", department_id=department.id)


@login_required
@require_http_methods(["GET", "POST"])
def publication_lifecycle_modal(request: HttpRequest, publication_id, action: str) -> HttpResponse:
    action_config = {
        "delete-staged": {
            "title": "Delete staged publication",
            "button": "Delete staged publication",
            "button_class": "btn-danger",
            "description": (
                "This staged publication has not been distributed. Deleting it abandons this "
                "publication attempt. Its version remains in publication history and is not reused."
            ),
            "service": delete_staged_publication,
        },
        "cancel": {
            "title": "Cancel build",
            "button": "Cancel build",
            "button_class": "btn-warning",
            "description": (
                "The build is currently running. If it finishes before cancellation takes effect, "
                "the new version is published normally."
            ),
            "service": cancel_publication_build,
        },
        "rollback": {
            "title": "Roll back publication",
            "button": "Roll back",
            "button_class": "btn-warning",
            "description": (
                "This successful historical version becomes the current distributed publication."
            ),
            "service": rollback_publication,
        },
        "delete": {
            "title": "Delete publication",
            "button": "Delete publication",
            "button_class": "btn-danger",
            "description": (
                "The historical publication record remains, but its artifact becomes unavailable "
                "and "
                "the version can no longer be restored."
            ),
            "service": delete_publication,
        },
    }
    if action not in action_config:
        raise PermissionDenied("Unknown publication lifecycle action.")
    publication = get_object_or_404(
        DatasetPublication.objects.select_related("department", "station", "scope_state"),
        pk=publication_id,
    )
    require_department_admin(request.user, publication.department)
    config = action_config[action]
    context = {
        "publication": publication,
        "scope_title": _scope_title(publication.scope_state),
        "action": action,
        "modal_title": config["title"],
        "submit_label": config["button"],
        "submit_class": config["button_class"],
        "description": config["description"],
        "modal_container_id": "publication-action-modal-container",
    }
    if request.method == "POST":
        return_url = reverse("publications-scope-detail", args=(publication.scope_state_id,))
        require_recent_reauthentication(request, return_url=return_url)
        try:
            config["service"](actor=request.user, publication=publication)
        except PublicationError as caught_error:
            publication.refresh_from_db(fields=("status",))
            error_message = str(caught_error)
            if action == "cancel" and publication.status == DatasetPublication.Status.PUBLISHED:
                error_message = (
                    f"Publication v{publication.version_number} finished before cancellation could "
                    "take effect and is now current."
                )
            return render(
                request, "publications/_lifecycle_modal.html", context | {"error": error_message}
            )
        return _modal_redirect(request, return_url)
    return render(request, "publications/_lifecycle_modal.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def publication_review(request: HttpRequest, publication_id) -> HttpResponse:
    """Compatibility route for bookmarked version review URLs."""
    publication = get_object_or_404(
        DatasetPublication.objects.select_related("department", "scope_state"),
        pk=publication_id,
        department_id__in=active_department_ids(request.user),
    )
    require_department_admin(request.user, publication.department)
    return redirect("publications-scope-detail", scope_id=publication.scope_state_id)
