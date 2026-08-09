from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids
from apps.authorization.services import require_department_admin
from apps.organizations.models import Department
from apps.reference_data.forms import ActiveForm, FirePlanUploadForm, HydrantForm, HydrantImportForm
from apps.reference_data.hydrants import HydrantImportError
from apps.reference_data.models import FirePlan, Hydrant, HydrantImportPreview
from apps.reference_data.services import (
    ReferenceDataError,
    accept_fire_plan,
    confirm_hydrant_preview,
    create_hydrant,
    create_hydrant_preview,
    set_fire_plan_active,
    update_hydrant,
)


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
@require_http_methods(["GET", "POST"])
def hydrants(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = HydrantImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = create_hydrant_preview(
                actor=request.user,
                department=department,
                raw_geojson=form.cleaned_data["geojson"].read(),
            )
        except HydrantImportError as error:
            form.add_error("geojson", str(error))
        else:
            return redirect(
                "reference-data-hydrant-preview", department_id=department.id, preview_id=preview.id
            )
    return render(
        request,
        "reference_data/hydrants.html",
        {
            "department": department,
            "hydrants": Hydrant.objects.filter(department=department),
            "form": form,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_preview(request: HttpRequest, department_id, preview_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    preview = get_object_or_404(
        HydrantImportPreview,
        pk=preview_id,
        department=department,
        created_by=request.user,
    )
    if request.method == "POST":
        try:
            created, updated, duplicates = confirm_hydrant_preview(
                actor=request.user, department=department, preview_id=preview.id
            )
        except PermissionDenied:
            raise
        messages.success(
            request, f"Imported {created} hydrants; updated {updated}; duplicates: {duplicates}."
        )
        return redirect("reference-data-hydrants", department_id=department.id)
    return render(
        request,
        "reference_data/hydrant_import_preview.html",
        {"department": department, "preview": preview},
    )


@login_required
@require_http_methods(["GET", "POST"])
def hydrant_create(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = HydrantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        create_hydrant(
            actor=request.user,
            department=department,
            longitude=data.pop("longitude"),
            latitude=data.pop("latitude"),
            **data,
        )
        return redirect("reference-data-hydrants", department_id=department.id)
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
        data = form.cleaned_data
        hydrant.location = Point(data.pop("longitude"), data.pop("latitude"), srid=4326)
        update_hydrant(actor=request.user, hydrant=hydrant, **data)
        return redirect("reference-data-hydrants", department_id=hydrant.department_id)
    return render(request, "reference_data/hydrant_manage.html", {"hydrant": hydrant, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def fire_plans(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = FirePlanUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        location = None
        if data["longitude"] is not None:
            location = Point(data.pop("longitude"), data.pop("latitude"), srid=4326)
        else:
            data.pop("longitude")
            data.pop("latitude")
        try:
            accept_fire_plan(
                actor=request.user,
                department=department,
                uploaded_file=data.pop("document"),
                location=location,
                **data,
            )
        except ReferenceDataError:
            form.add_error("document", "Fire plan was rejected.")
        else:
            return redirect("reference-data-fire-plans", department_id=department.id)
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
