from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
from apps.publications.forms import RebuildRequestForm
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import (
    PublicationError,
    publish_publication,
    reject_publication,
    request_rebuild,
    rollback_publication,
)


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    return department


@login_required
@require_http_methods(["GET", "POST"])
def publications(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = RebuildRequestForm(request.POST or None, department=department)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request)
        request_rebuild(actor=request.user, department=department, **form.cleaned_data)
        messages.success(request, "Publication rebuild requested.")
        return redirect("publications-list", department_id=department.id)
    return render(
        request,
        "publications/list.html",
        {
            "department": department,
            "form": form,
            "scopes": DatasetScopeState.objects.filter(department=department).select_related(
                "station", "latest_built_publication", "current_published_publication"
            ),
            "jobs": PublicationJob.objects.filter(department=department).select_related(
                "station", "build_publication"
            )[:25],
            "publications": DatasetPublication.objects.filter(department=department).select_related(
                "station", "created_by", "published_by"
            )[:50],
        },
    )


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
        require_recent_reauthentication(request)
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
    return render(request, "publications/review.html", {"publication": publication})
