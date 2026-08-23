from typing import cast
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import ImportError, create_single_preview
from apps.organizations.models import Department
from apps.reference_data.forms import (
    DocumentFilterForm,
    FirePlanEditForm,
    FirePlanUploadForm,
    HydrantEditForm,
    HydrantFilterForm,
    HydrantForm,
    KlgvPlanEditForm,
    KlgvPlanUploadForm,
)
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan
from apps.reference_data.services import (
    delete_fire_plan,
    delete_hydrant,
    delete_klgv_plan,
    set_fire_plan_active,
    set_klgv_plan_active,
    update_fire_plan,
    update_hydrant,
    update_klgv_plan,
)

HYDRANT_LIST_PAGE_SIZE = 100
DOCUMENT_LIST_PAGE_SIZE = 100


def _modal(request: HttpRequest, template: str, context: dict[str, object]) -> HttpResponse:
    return render(request, template, context)


def _modal_redirect(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


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
    if "status" not in request.GET:
        queryset = queryset.filter(status=Hydrant.Status.ACTIVE)
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
            "recent_batches": ImportBatch.objects.filter(
                department=department, domain=ImportBatch.Domain.HYDRANTS
            ).order_by("-created_at")[:10],
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
    return render(request, "reference_data/hydrant_manage.html", {"hydrant": hydrant})


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_edit_modal(request: HttpRequest, hydrant_id) -> HttpResponse:
    hydrant = get_object_or_404(
        Hydrant, pk=hydrant_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, hydrant.department)
    form = HydrantEditForm(
        request.POST or None,
        initial={
            "external_identifier": hydrant.external_identifier,
            "longitude": hydrant.location.x,
            "latitude": hydrant.location.y,
            "hydrant_type": hydrant.hydrant_type,
            "flow_information": hydrant.flow_information,
            "diameter_mm": hydrant.diameter_mm,
        },
    )
    if request.method == "POST" and form.is_valid():
        update_hydrant(actor=request.user, hydrant=hydrant, **form.cleaned_data)
        return _modal_redirect(request, reverse("reference-data-hydrant-manage", args=[hydrant.id]))
    return _modal(request, "reference_data/_hydrant_form_modal.html", {"form": form})


@login_required
@require_http_methods(["POST"])
def hydrant_lifecycle(request: HttpRequest, hydrant_id) -> HttpResponse:
    hydrant = get_object_or_404(
        Hydrant, pk=hydrant_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, hydrant.department)
    status = request.POST.get("status")
    if status not in {Hydrant.Status.ACTIVE, Hydrant.Status.INACTIVE}:
        return HttpResponse(status=400)
    update_hydrant(actor=request.user, hydrant=hydrant, status=status)
    return cast(HttpResponse, redirect("reference-data-hydrant-manage", hydrant_id=hydrant.id))


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_delete_modal(request: HttpRequest, hydrant_id) -> HttpResponse:
    hydrant = get_object_or_404(
        Hydrant, pk=hydrant_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, hydrant.department)
    if request.method == "POST":
        try:
            delete_hydrant(actor=request.user, hydrant=hydrant)
        except ValueError as error:
            return _modal(
                request,
                "portal/_delete_modal.html",
                {
                    "object": hydrant,
                    "error": str(error),
                    "action_url": request.path,
                    "modal_container_id": "hydrant-action-modal-container",
                },
            )
        return _modal_redirect(
            request, reverse("reference-data-hydrants", args=[hydrant.department_id])
        )
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": hydrant,
            "action_url": request.path,
            "modal_container_id": "hydrant-action-modal-container",
        },
    )


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
                    "fsd_location": data["fsd_location"],
                    "bmz_location": data["bmz_location"],
                    "rwa_info": data["rwa_info"],
                },
                pdf_bytes=document.read(),
                original_filename=document.name,
            )
        except ImportError:
            form.add_error("document", "Fire plan was rejected.")
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    filters = DocumentFilterForm(request.GET or None)
    queryset = FirePlan.objects.filter(department=department)
    if filters.is_valid():
        if filters.cleaned_data["q"]:
            query = filters.cleaned_data["q"]
            queryset = queryset.filter(
                Q(external_identifier__icontains=query)
                | Q(object_name__icontains=query)
                | Q(address__icontains=query)
                | Q(city__icontains=query)
            )
        if filters.cleaned_data["active"]:
            queryset = queryset.filter(active=filters.cleaned_data["active"] == "active")
    if "active" not in request.GET:
        queryset = queryset.filter(active=True)
    paginator = Paginator(
        queryset.order_by("object_name", "address", "id"), DOCUMENT_LIST_PAGE_SIZE
    )
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = urlencode(
        [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
    )
    return render(
        request,
        "reference_data/fire_plans.html",
        {
            "department": department,
            "fire_plans": page.object_list,
            "form": form,
            "filter_form": filters,
            "page": page,
            "total_count": paginator.count,
            "page_query": page_query,
            "recent_batches": ImportBatch.objects.filter(
                department=department, domain=ImportBatch.Domain.FIRE_PLANS
            ).order_by("-created_at")[:10],
        },
    )


@login_required
@require_http_methods(["GET"])
def fire_plan_detail(request: HttpRequest, fire_plan_id) -> HttpResponse:
    fire_plan = get_object_or_404(
        FirePlan, pk=fire_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, fire_plan.department)
    return render(request, "reference_data/fire_plan_detail.html", {"fire_plan": fire_plan})


@login_required
@require_http_methods(["GET"])
def klgv_plan_detail(request: HttpRequest, klgv_plan_id) -> HttpResponse:
    plan = get_object_or_404(
        KlgvPlan, pk=klgv_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, plan.department)
    return render(request, "reference_data/klgv_plan_detail.html", {"plan": plan})


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
    filters = DocumentFilterForm(request.GET or None)
    queryset = KlgvPlan.objects.filter(department=department)
    if filters.is_valid():
        if filters.cleaned_data["q"]:
            query = filters.cleaned_data["q"]
            queryset = queryset.filter(
                Q(external_identifier__icontains=query)
                | Q(title__icontains=query)
                | Q(category__icontains=query)
            )
        if filters.cleaned_data["active"]:
            queryset = queryset.filter(active=filters.cleaned_data["active"] == "active")
    if "active" not in request.GET:
        queryset = queryset.filter(active=True)
    paginator = Paginator(
        queryset.order_by("title", "external_identifier", "id"), DOCUMENT_LIST_PAGE_SIZE
    )
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = urlencode(
        [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
    )
    return render(
        request,
        "reference_data/klgv_plans.html",
        {
            "department": department,
            "plans": page.object_list,
            "form": form,
            "filter_form": filters,
            "page": page,
            "total_count": paginator.count,
            "page_query": page_query,
            "recent_batches": ImportBatch.objects.filter(
                department=department, domain=ImportBatch.Domain.KLGV_PLANS
            ).order_by("-created_at")[:10],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def fire_plan_edit_modal(request: HttpRequest, fire_plan_id) -> HttpResponse:
    fire_plan = get_object_or_404(
        FirePlan, pk=fire_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, fire_plan.department)
    form = FirePlanEditForm(request.POST or None, instance=fire_plan)
    if request.method == "POST" and form.is_valid():
        update_fire_plan(actor=request.user, fire_plan=fire_plan, **form.cleaned_data)
        return _modal_redirect(
            request, reverse("reference-data-fire-plan-detail", args=[fire_plan.id])
        )
    return _modal(request, "reference_data/_fire_plan_form_modal.html", {"form": form})


@login_required
@require_http_methods(["POST"])
def fire_plan_lifecycle(request: HttpRequest, fire_plan_id) -> HttpResponse:
    fire_plan = get_object_or_404(
        FirePlan, pk=fire_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, fire_plan.department)
    requested = request.POST.get("active")
    if requested not in {"true", "false"}:
        return HttpResponse(status=400)
    set_fire_plan_active(actor=request.user, fire_plan=fire_plan, active=requested == "true")
    return cast(
        HttpResponse, redirect("reference-data-fire-plan-detail", fire_plan_id=fire_plan.id)
    )


@login_required
@require_http_methods(["GET", "POST"])
def fire_plan_delete_modal(request: HttpRequest, fire_plan_id) -> HttpResponse:
    fire_plan = get_object_or_404(
        FirePlan, pk=fire_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, fire_plan.department)
    if request.method == "POST":
        try:
            delete_fire_plan(actor=request.user, fire_plan=fire_plan)
        except ValueError as error:
            return _modal(
                request,
                "portal/_delete_modal.html",
                {
                    "object": fire_plan,
                    "error": str(error),
                    "action_url": request.path,
                    "modal_container_id": "fire-plan-action-modal-container",
                },
            )
        return _modal_redirect(
            request, reverse("reference-data-fire-plans", args=[fire_plan.department_id])
        )
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": fire_plan,
            "action_url": request.path,
            "modal_container_id": "fire-plan-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def klgv_plan_edit_modal(request: HttpRequest, klgv_plan_id) -> HttpResponse:
    plan = get_object_or_404(
        KlgvPlan, pk=klgv_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, plan.department)
    form = KlgvPlanEditForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        update_klgv_plan(actor=request.user, klgv_plan=plan, **form.cleaned_data)
        return _modal_redirect(request, reverse("reference-data-klgv-plan-detail", args=[plan.id]))
    return _modal(request, "reference_data/_klgv_plan_form_modal.html", {"form": form})


@login_required
@require_http_methods(["POST"])
def klgv_plan_lifecycle(request: HttpRequest, klgv_plan_id) -> HttpResponse:
    plan = get_object_or_404(
        KlgvPlan, pk=klgv_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, plan.department)
    requested = request.POST.get("active")
    if requested not in {"true", "false"}:
        return HttpResponse(status=400)
    set_klgv_plan_active(actor=request.user, klgv_plan=plan, active=requested == "true")
    return cast(HttpResponse, redirect("reference-data-klgv-plan-detail", klgv_plan_id=plan.id))


@login_required
@require_http_methods(["GET", "POST"])
def klgv_plan_delete_modal(request: HttpRequest, klgv_plan_id) -> HttpResponse:
    plan = get_object_or_404(
        KlgvPlan, pk=klgv_plan_id, department_id__in=active_department_ids(request.user)
    )
    require_department_admin(request.user, plan.department)
    if request.method == "POST":
        try:
            delete_klgv_plan(actor=request.user, klgv_plan=plan)
        except ValueError as error:
            return _modal(
                request,
                "portal/_delete_modal.html",
                {
                    "object": plan,
                    "error": str(error),
                    "action_url": request.path,
                    "modal_container_id": "klgv-action-modal-container",
                },
            )
        return _modal_redirect(
            request, reverse("reference-data-klgv-plans", args=[plan.department_id])
        )
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": plan,
            "action_url": request.path,
            "modal_container_id": "klgv-action-modal-container",
        },
    )
