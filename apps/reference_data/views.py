from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import ImportError, create_single_preview
from apps.organizations.models import Department
from apps.reference_data.forms import (
    ActiveForm,
    FirePlanUploadForm,
    HydrantFilterForm,
    HydrantForm,
    KlgvPlanUploadForm,
)
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan
from apps.reference_data.services import set_fire_plan_active, set_klgv_plan_active

HYDRANT_LIST_PAGE_SIZE = 100


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    try:
        require_department_admin(request.user, department)
    except PermissionDenied:
        record_event(
            action="reference_data.unauthorized_access",
            request=request,
            actor_user=request.user,
            department=department,
            target_type="reference_data",
        )
        raise
    return department


@login_required
@require_http_methods(["GET"])
def hydrants(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = HydrantFilterForm(request.GET or None)
    queryset = Hydrant.objects.filter(department=department).order_by("external_identifier", "id")
    if form.is_valid():
        data = form.cleaned_data
        if data.get("q"):
            queryset = queryset.filter(external_identifier__icontains=data["q"])
        if data.get("status"):
            queryset = queryset.filter(status=data["status"])
        if data.get("hydrant_type"):
            queryset = queryset.filter(hydrant_type__icontains=data["hydrant_type"])
        if data.get("diameter_mm"):
            queryset = queryset.filter(diameter_mm=data["diameter_mm"])
    paginator = Paginator(queryset, HYDRANT_LIST_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = urlencode(
        [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
    )
    return render(
        request,
        "reference_data/hydrants.html",
        {
            "department": department,
            "form": form,
            "hydrants": page.object_list,
            "page": page,
            "total_count": paginator.count,
            "page_query": page_query,
            "status_choices": Hydrant.Status.choices,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_create(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = HydrantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_single_preview(
                actor=request.user,
                department=department,
                domain=ImportBatch.Domain.HYDRANTS,
                values=form.cleaned_data,
                original_filename="manual-hydrant-v1.csv",
            )
        except ImportError as error:
            form.add_error(None, str(error))
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    return render(
        request, "reference_data/hydrant_create.html", {"department": department, "form": form}
    )


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_manage(request: HttpRequest, hydrant_id) -> HttpResponse:
    hydrant = get_object_or_404(
        Hydrant, pk=hydrant_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, hydrant.department)
    form = HydrantForm(
        request.POST or None,
        initial={
            "external_identifier": hydrant.external_identifier,
            "longitude": hydrant.location.x,
            "latitude": hydrant.location.y,
            "hydrant_type": hydrant.hydrant_type,
            "diameter_mm": hydrant.diameter_mm,
            "status": hydrant.status,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_single_preview(
                actor=request.user,
                department=hydrant.department,
                domain=ImportBatch.Domain.HYDRANTS,
                values=form.cleaned_data,
                original_filename="manual-hydrant-v1.csv",
            )
        except ImportError as error:
            form.add_error(None, str(error))
        else:
            return redirect(
                "ingestion-preview", department_id=hydrant.department_id, batch_id=batch.id
            )
    return render(request, "reference_data/hydrant_manage.html", {"hydrant": hydrant, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def fire_plans(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = FirePlanUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            document = data["document"]
            batch = create_single_preview(
                actor=request.user,
                department=department,
                domain=ImportBatch.Domain.FIRE_PLANS,
                values={
                    "external_identifier": data["external_identifier"],
                    "object_name": data["object_name"],
                    "address": data["address"],
                    "postal_code": data["postal_code"],
                    "city": data["city"],
                    "longitude": data["longitude"] or "",
                    "latitude": data["latitude"] or "",
                },
                pdf_bytes=document.read(),
                original_filename=document.name,
            )
        except ImportError:
            form.add_error("document", "Fire plan was rejected.")
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    return render(
        request,
        "reference_data/fire_plans.html",
        {
            "department": department,
            "fire_plans": FirePlan.objects.filter(department=department),
            "form": form,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def fire_plan_detail(request: HttpRequest, fire_plan_id) -> HttpResponse:
    fire_plan = get_object_or_404(
        FirePlan, pk=fire_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, fire_plan.department)
    form = ActiveForm(request.POST or None, initial={"active": fire_plan.active})
    if request.method == "POST" and form.is_valid():
        set_fire_plan_active(
            actor=request.user, fire_plan=fire_plan, active=form.cleaned_data["active"]
        )
        return redirect("reference-data-fire-plans", department_id=fire_plan.department_id)
    return render(
        request, "reference_data/fire_plan_detail.html", {"fire_plan": fire_plan, "form": form}
    )


@login_required
@require_http_methods(["GET", "POST"])
def klgv_plan_detail(request: HttpRequest, klgv_plan_id) -> HttpResponse:
    plan = get_object_or_404(
        KlgvPlan, pk=klgv_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, plan.department)
    form = ActiveForm(request.POST or None, initial={"active": plan.active})
    if request.method == "POST" and form.is_valid():
        set_klgv_plan_active(actor=request.user, klgv_plan=plan, active=form.cleaned_data["active"])
        return redirect("reference-data-klgv-plans", department_id=plan.department_id)
    return render(request, "reference_data/klgv_plan_detail.html", {"plan": plan, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def klgv_plans(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = KlgvPlanUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        document = data["document"]
        try:
            batch = create_single_preview(
                actor=request.user,
                department=department,
                domain=ImportBatch.Domain.KLGV_PLANS,
                values={
                    "external_id": data["external_id"],
                    "title": data["title"],
                    "category": data["category"],
                },
                pdf_bytes=document.read(),
                original_filename=document.name,
            )
        except ImportError:
            form.add_error("document", "KLGV plan was rejected.")
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    return render(
        request,
        "reference_data/klgv_plans.html",
        {
            "department": department,
            "plans": KlgvPlan.objects.filter(department=department),
            "form": form,
        },
    )
