from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
from apps.tablets.models import AppInstallation, Tablet


@require_http_methods(["GET"])
@login_required
def tablet_list(request: HttpRequest, department_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)

    tablets = Tablet.objects.filter(department=department).prefetch_related(
        Prefetch(
            "installations",
            queryset=AppInstallation.objects.filter(
                status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
            ),
            to_attr="current_installations",
        )
    )
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if query:
        tablets = tablets.filter(
            Q(display_name__icontains=query) | Q(asset_number__icontains=query)
        )
    if status:
        tablets = tablets.filter(status=status)

    return render(
        request,
        "tablets/list.html",
        {
            "department": department,
            "tablets": tablets.order_by("display_name", "asset_number"),
            "query": query,
            "status": status,
            "statuses": Tablet.Status.choices,
        },
    )


@require_http_methods(["GET"])
@login_required
def tablet_detail(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    tablet = get_object_or_404(Tablet, pk=tablet_id, department=department)

    return render(
        request,
        "tablets/detail.html",
        {
            "department": department,
            "tablet": tablet,
            "installations": tablet.installations.order_by("-adopted_at"),
        },
    )
