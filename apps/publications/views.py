from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.diffs import source_diff
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.publications.services import (
    PublicationError,
    build_staged_publication,
    bulk_request_rebuilds,
    cancel_publication_build,
    cut_over_fire_plan_scope_to_document_manifest,
    delete_publication,
    delete_staged_publication,
    request_rebuild,
    rollback_publication,
    stage_publication_update,
)
from apps.publications.state import (
    BUILDING,
    FAILED,
    NEEDS_REBUILD,
    NOT_PUBLISHED,
    QUEUED,
    UPDATE_QUEUED,
    publication_scope_queryset,
    scope_operational_states_for_scopes,
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
    requires_candidate_snapshot = state in (UPDATE_QUEUED, QUEUED)
    can_inspect = bool(row["current_snapshot_retained"]) and (
        not requires_candidate_snapshot or bool(row["latest_snapshot_retained"])
    )
    return {
        "delete_staged": state in (UPDATE_QUEUED, QUEUED)
        and (row["active_job_publication_id"] or row["latest_publication_id"]) is not None,
        "cancel_build": state == BUILDING
        and (row["active_job_publication_id"] or row["latest_publication_id"]) is not None,
        "build_now": state in (UPDATE_QUEUED, QUEUED)
        and (row["active_job_publication_id"] or row["latest_publication_id"]) is not None,
        "stage_update": state in (FAILED, NEEDS_REBUILD, NOT_PUBLISHED),
        "inspect_changes": can_inspect and state in (FAILED, NEEDS_REBUILD, UPDATE_QUEUED, QUEUED),
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


def _filtered_scope_queryset(department: Department, selected_filter: str):
    """Filter scope rows before pagination without materializing every row.

    The normal list path has no publication subqueries at all.  State filters
    need a small number of boolean/latest-status predicates for an accurate
    count, but intentionally never project a publication's heavy payload.
    """
    queryset = publication_scope_queryset(department)
    if not selected_filter:
        return queryset

    active_jobs = PublicationJob.objects.filter(
        scope_state_id=OuterRef("pk"),
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    )
    running_jobs = active_jobs.filter(status=PublicationJob.Status.RUNNING)
    pending_jobs = active_jobs.filter(status=PublicationJob.Status.PENDING)
    latest = DatasetPublication.objects.filter(scope_state_id=OuterRef("pk")).order_by(
        "-version_number"
    )
    queryset = queryset.annotate(
        has_running_job=Exists(running_jobs),
        has_pending_job=Exists(pending_jobs),
        latest_status=Subquery(latest.values("status")[:1]),
    )
    dirty = (
        Q(current_published_publication__isnull=True)
        | Q(current_source_fingerprint="")
        | Q(current_published_publication__source_fingerprint="")
        | ~Q(current_source_fingerprint=F("current_published_publication__source_fingerprint"))
    )
    no_active_job = Q(has_running_job=False, has_pending_job=False)
    if selected_filter == "building":
        return queryset.filter(
            Q(has_running_job=True) | Q(latest_status=DatasetPublication.Status.BUILDING)
        )
    if selected_filter == "scheduled":
        return queryset.filter(
            Q(has_running_job=False)
            & (Q(has_pending_job=True) | Q(latest_status=DatasetPublication.Status.STAGED))
        )
    if selected_filter == "failed":
        # Failed is current only when the latest failed attempt belongs to the
        # current source. This extra scalar exists only for the explicit
        # Failed filter; normal list rendering remains page-first and batched.
        queryset = queryset.annotate(
            latest_source_fingerprint=Subquery(latest.values("source_fingerprint")[:1])
        )
        return (
            queryset.filter(no_active_job, latest_status=DatasetPublication.Status.FAILED)
            .filter(dirty)
            .filter(
                Q(latest_source_fingerprint="")
                | Q(latest_source_fingerprint=F("current_source_fingerprint"))
            )
        )
    if selected_filter == "current":
        return (
            queryset.filter(
                no_active_job,
                current_published_publication__isnull=False,
                current_source_fingerprint=F("current_published_publication__source_fingerprint"),
            )
            .exclude(
                latest_status__in=(
                    DatasetPublication.Status.STAGED,
                    DatasetPublication.Status.BUILDING,
                )
            )
            .exclude(latest_built_publication__status=DatasetPublication.Status.READY_FOR_REVIEW)
        )
    if selected_filter == "not_published":
        return queryset.filter(
            no_active_job,
            current_published_publication__isnull=True,
            latest_status__isnull=True,
        )
    return queryset


def _publication_list_context(request: HttpRequest, department: Department) -> dict[str, object]:
    selected_filter = request.GET.get("state", "")
    if selected_filter not in SCOPE_FILTERS:
        selected_filter = ""
    paginator = Paginator(_filtered_scope_queryset(department, selected_filter), SCOPE_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    rows = _decorate_scope_rows(scope_operational_states_for_scopes(list(page.object_list)))
    page_query = request.GET.copy()
    page_query.pop("page", None)
    return {
        "department": department,
        "scope_rows": rows,
        "page": page,
        "total_count": paginator.count,
        "selected_filter": selected_filter,
        "scope_filters": SCOPE_FILTERS,
        "page_query": page_query.urlencode(),
    }


def _scope_row_context(scope: DatasetScopeState) -> dict[str, object]:
    rows = _decorate_scope_rows(scope_operational_states_for_scopes([scope]))
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
@require_http_methods(["GET"])
def publication_inspect_changes(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    row = _scope_row_context(scope)["row"]
    candidate = None
    if row["state"] in (UPDATE_QUEUED, QUEUED):
        candidate = DatasetPublication.objects.filter(pk=row["update_publication_id"]).first()
    current = scope.current_published_publication
    if current is None or current.source_snapshot is None:
        return render(
            request,
            "publications/_inspect_changes_modal.html",
            {"scope": scope, "scope_title": _scope_title(scope), "legacy": True},
        )
    if candidate is not None and candidate.source_snapshot is None:
        return render(
            request,
            "publications/_inspect_changes_modal.html",
            {"scope": scope, "scope_title": _scope_title(scope), "legacy": True},
        )
    target_snapshot = (
        candidate.source_snapshot
        if candidate is not None
        else build_source_payload(
            definition=get_dataset_definition(scope.dataset_type_code),
            department=scope.department,
            station=scope.station,
        )
    )
    return render(
        request,
        "publications/_inspect_changes_modal.html",
        {
            "scope": scope,
            "scope_title": _scope_title(scope),
            "candidate": candidate,
            "diff": source_diff(current.source_snapshot, target_snapshot),
        },
    )


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
def scope_fire_plan_document_manifest_cutover(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    return_url = reverse("publications-scope-detail", args=(scope.id,))
    require_recent_reauthentication(request, return_url=return_url)
    try:
        cut_over_fire_plan_scope_to_document_manifest(actor=request.user, scope=scope)
    except PublicationError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request, "Document-manifest v2 cutover requested; the first build is queued."
        )
    return redirect("publications-scope-detail", scope_id=scope.id)


def _scope_mutation_response(request: HttpRequest, scope: DatasetScopeState) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        return render(request, "publications/_scope_row.html", _scope_row_context(scope))
    return redirect("publications-scope-detail", scope_id=scope.id)


@login_required
@require_http_methods(["POST"])
def scope_stage_update(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    require_recent_reauthentication(
        request, return_url=reverse("publications-scope-detail", args=(scope.id,))
    )
    try:
        stage_publication_update(
            actor=request.user,
            department=scope.department,
            station=scope.station,
            dataset_type_code=scope.dataset_type_code,
        )
    except PublicationError as error:
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(str(error), status=409)
        messages.info(request, str(error))
    return _scope_mutation_response(request, scope)


@login_required
@require_http_methods(["POST"])
def scope_build_now(request: HttpRequest, scope_id) -> HttpResponse:
    scope = _scope_or_403(request, scope_id)
    require_recent_reauthentication(
        request, return_url=reverse("publications-scope-detail", args=(scope.id,))
    )
    try:
        build_staged_publication(actor=request.user, scope=scope)
    except PublicationError as error:
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(str(error), status=409)
        messages.info(request, str(error))
    return _scope_mutation_response(request, scope)


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
                "publication attempt. Changes to the underlying canonical data are not undone. "
                "If they still differ from the current publication, this scope remains marked "
                "as Changes not published. Its version remains in publication history and is "
                "not reused."
            ),
            "service": delete_staged_publication,
        },
        "cancel": {
            "title": "Cancel build",
            "button": "Cancel build",
            "button_class": "btn-warning",
            "description": (
                "The build is currently running. If it finishes before cancellation takes effect, "
                "the new version is published normally. Cancelling does not undo underlying "
                "canonical data changes; they remain Changes not published until staged again."
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
