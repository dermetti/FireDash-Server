from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.forms import ModelChoiceField
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.authorization.services import require_department_admin
from apps.ingestion.forms import (
    FirePlanCoordinateReviewForm,
    ImportUploadForm,
    StationVehicleResolutionForm,
)
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    approve_all_review_decisions,
    cancel_preview,
    create_preview,
    review_context,
    set_personnel_home_station_resolution,
    set_review_coordinates,
    set_review_decision,
    set_station_vehicle_resolution,
)
from apps.organizations.models import Department, Station


def _department(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    return department


def _imports_url(*, department: Department, domain: str) -> str:
    """Return the canonical, domain-scoped import landing page.

    A preview already records its domain.  Returning through that durable state
    avoids both user-controlled continuation URLs and the historical hydrants
    fallback after a successful document import.
    """
    names = {
        ImportBatch.Domain.HYDRANTS: "ingestion-import-hydrants",
        ImportBatch.Domain.PERSONNEL: "ingestion-import-personnel",
        ImportBatch.Domain.FIRE_PLANS: "ingestion-import-fire-plans",
        ImportBatch.Domain.KLGV_PLANS: "ingestion-import-klgv-plans",
        ImportBatch.Domain.STATION_VEHICLES: "ingestion-import-station-vehicles",
    }
    return reverse(names[domain], args=(department.id,))


def _domain_import(request: HttpRequest, department_id, domain: str) -> HttpResponse:
    """Domain routes remain thin adapters over the single authoritative wizard."""
    request.GET = request.GET.copy()
    request.GET["domain"] = domain
    if request.method == "POST":
        request.POST = request.POST.copy()
        request.POST["domain"] = domain
    return imports(request, department_id)


@login_required
@require_http_methods(["GET", "POST"])
def import_hydrants(request: HttpRequest, department_id) -> HttpResponse:
    return _domain_import(request, department_id, ImportBatch.Domain.HYDRANTS)


@login_required
@require_http_methods(["GET", "POST"])
def import_personnel(request: HttpRequest, department_id) -> HttpResponse:
    return _domain_import(request, department_id, ImportBatch.Domain.PERSONNEL)


@login_required
@require_http_methods(["GET", "POST"])
def import_fire_plans(request: HttpRequest, department_id) -> HttpResponse:
    return _domain_import(request, department_id, ImportBatch.Domain.FIRE_PLANS)


@login_required
@require_http_methods(["GET", "POST"])
def import_klgv_plans(request: HttpRequest, department_id) -> HttpResponse:
    return _domain_import(request, department_id, ImportBatch.Domain.KLGV_PLANS)


@login_required
@require_http_methods(["GET", "POST"])
def import_station_vehicles(request: HttpRequest, department_id) -> HttpResponse:
    return _domain_import(request, department_id, ImportBatch.Domain.STATION_VEHICLES)


@login_required
@require_http_methods(["GET", "POST"])
def imports(request: HttpRequest, department_id) -> HttpResponse:
    department = _department(request, department_id)
    requested_domain = request.POST.get("domain") or request.GET.get("domain")
    domain_configs = {
        ImportBatch.Domain.HYDRANTS: {
            "formats": (
                (ImportBatch.Format.GEOJSON, "GeoJSON"),
                (ImportBatch.Format.CSV, "CSV"),
            ),
            "mode": ImportBatch.Mode.MERGE,
            "help": (
                "GeoJSON is preferred. CSV is also accepted; coordinates use EPSG:4326 "
                "longitude then latitude."
            ),
            "template": "ingestion/hydrants_import.html",
        },
        ImportBatch.Domain.PERSONNEL: {
            "formats": ((ImportBatch.Format.CSV, "CSV"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "CSV uses a Home Station Short Code or full Station name. Upsert creates or "
                "updates stable personnel-number matches; absence never offboards people or "
                "ends assignments."
            ),
            "template": "ingestion/personnel_import.html",
        },
        ImportBatch.Domain.FIRE_PLANS: {
            "formats": ((ImportBatch.Format.ZIP, "ZIP package"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "ZIP packages use fire-plans-manifest-v1.csv. An upsert creates a plan when "
                "its External ID/address identity is new, otherwise it updates that plan."
            ),
            "template": "ingestion/fire_plans_import.html",
        },
        ImportBatch.Domain.KLGV_PLANS: {
            "formats": ((ImportBatch.Format.ZIP, "ZIP package"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "ZIP packages use manifest.csv. Object name, address, postal code, and city "
                "are required; coordinates are optional staged review data."
            ),
            "template": "ingestion/klgv_plans_import.html",
        },
        ImportBatch.Domain.STATION_VEHICLES: {
            "formats": ((ImportBatch.Format.CSV, "CSV"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "Use station and vehicle rows in one CSV. Vehicle Station references resolve by "
                "Short Code first, then full Station name; unresolved references are reviewed "
                "before final Apply. Omitting a Vehicle never retires it."
            ),
            "template": "ingestion/station_vehicles_import.html",
        },
    }
    target_domain = requested_domain if requested_domain in domain_configs else None
    initial = {
        key: request.GET[key]
        for key in ("domain", "import_format", "import_mode")
        if key in request.GET
    }
    form = ImportUploadForm(request.POST or None, request.FILES or None, initial=initial)
    if target_domain:
        config = domain_configs[target_domain]
        form.fields["domain"].choices = ((target_domain, ImportBatch.Domain(target_domain).label),)
        form.fields["import_format"].choices = config["formats"]
        form.fields["import_mode"].choices = (
            (config["mode"], ImportBatch.Mode(config["mode"]).label),
        )
        form.fields["import_format"].initial = config["formats"][0][0]
        form.fields["import_mode"].initial = config["mode"]
    # Django's runtime form field classes are not PEP 585 generics.  Keep the
    # precise type for static checking without evaluating a subscription.
    station_field = cast("ModelChoiceField[Station]", form.fields["station"])
    station_field.queryset = Station.objects.filter(department=department, active=True)
    if request.method == "POST" and form.is_valid():
        source = form.cleaned_data["source"]
        try:
            batch = create_preview(
                actor=request.user,
                department=department,
                domain=form.cleaned_data["domain"],
                import_format=form.cleaned_data["import_format"],
                import_mode=form.cleaned_data["import_mode"],
                filename=source.name,
                payload=source.read(),
                station=form.cleaned_data["station"],
            )
        except ImportError as error:
            form.add_error("source", str(error))
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    template_name = (
        domain_configs[target_domain].get("template", "ingestion/imports.html")
        if target_domain
        else "ingestion/imports.html"
    )
    return render(
        request,
        template_name,
        {
            "department": department,
            "form": form,
            "batches": ImportBatch.objects.filter(
                department=department, **({"domain": target_domain} if target_domain else {})
            ).order_by("-created_at", "-id")[:20],
            "target_domain": target_domain,
            "domain_help": domain_configs[target_domain]["help"] if target_domain else None,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def preview(request: HttpRequest, department_id, batch_id) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    if request.method == "POST":
        try:
            applied = apply_preview(actor=request.user, batch_id=batch.id)
        except ImportError as error:
            messages.error(request, str(error))
        else:
            rejected = len(applied.validation_summary.get("document_failures", []))
            if rejected:
                messages.warning(
                    request,
                    f"Import applied with {rejected} rejected document(s); "
                    "publication is scheduled separately.",
                )
            else:
                messages.success(request, "Import applied; publication is scheduled separately.")
            return redirect(_imports_url(department=department, domain=batch.domain))
    review = _review_context(request, batch)
    context = {"department": department, "batch": batch, "review": review}
    if review is not None:
        context.update(_review_region_context(batch, review=review))
    return render(request, "ingestion/preview.html", context)


def _review_context(request: HttpRequest, batch: ImportBatch) -> dict[str, object] | None:
    if batch.domain not in {
        ImportBatch.Domain.FIRE_PLANS,
        ImportBatch.Domain.KLGV_PLANS,
        ImportBatch.Domain.STATION_VEHICLES,
        ImportBatch.Domain.PERSONNEL,
    }:
        return None
    requested_index = request.GET.get("review")
    try:
        index = int(requested_index) if requested_index is not None else None
    except (TypeError, ValueError):
        index = None
    return review_context(batch, index)


def _review_region_context(
    batch: ImportBatch,
    *,
    review: dict[str, object] | None = None,
    coordinate_data=None,
    coordinate_row_index: int | None = None,
) -> dict[str, object]:
    """Prepare the shared review fragment, including any staged coordinate form."""
    review = review or review_context(batch)
    coordinate_items = review["coordinate_items"]
    current_key = review["current"].get("key") if review["current"] else None
    coordinate_item = next(
        (
            item
            for item in coordinate_items
            if coordinate_row_index is not None and item["index"] == coordinate_row_index
        ),
        None,
    )
    if coordinate_item is None and current_key is not None:
        coordinate_item = next(
            (item for item in coordinate_items if item.get("review_key") == current_key), None
        )
    if coordinate_item is None and not review["current"]:
        coordinate_item = next(iter(coordinate_items), None)
    coordinate_form = None
    if coordinate_item is not None:
        coordinate_form = FirePlanCoordinateReviewForm(
            coordinate_data,
            longitude=coordinate_item["longitude"],
            latitude=coordinate_item["latitude"],
        )
    station_resolution_form = None
    personnel_resolution_form = None
    current = review.get("current")
    if batch.domain == ImportBatch.Domain.STATION_VEHICLES and isinstance(current, dict):
        kind = str(current.get("kind", ""))
        if kind in {"missing", "ambiguous"}:
            initial = {
                "short_code": "",
                "name": "",
            }
            source = next(
                (
                    row
                    for row in batch.normalized_intent.get("rows", [])
                    if isinstance(row, dict) and row.get("key") == current.get("key")
                ),
                {},
            )
            if isinstance(source, dict):
                initial.update(
                    {
                        "short_code": source.get("station_short_code", ""),
                        "name": source.get("station_name", ""),
                        "street": source.get("street", ""),
                        "house_number": source.get("house_number", ""),
                        "postal_code": source.get("postal_code", ""),
                        "city": source.get("city", ""),
                    }
                )
            station_resolution_form = StationVehicleResolutionForm(
                coordinate_data,
                department=batch.department,
                resolution_kind=kind,
                initial=initial,
            )
    if batch.domain == ImportBatch.Domain.PERSONNEL and isinstance(current, dict):
        if current.get("kind") == "personnel_ambiguous_home_station":
            personnel_resolution_form = StationVehicleResolutionForm(
                coordinate_data,
                department=batch.department,
                resolution_kind="ambiguous",
            )
    return {
        "batch": batch,
        "review": review,
        "coordinate_item": coordinate_item,
        "coordinate_form": coordinate_form,
        "station_resolution_form": station_resolution_form,
        "personnel_resolution_form": personnel_resolution_form,
    }


def _render_review_region(
    request: HttpRequest, department: Department, batch: ImportBatch, **kwargs
):
    return render(
        request,
        "ingestion/partials/_review_region.html",
        {"department": department, **_review_region_context(batch, **kwargs)},
    )


def _review_decide(request, department_id, batch_id, key, decision) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    try:
        review = review_context(batch)
        coordinate_item = next(
            (item for item in review["coordinate_items"] if item.get("review_key") == key),
            None,
        )
        coordinate_form = None
        if decision == "approved" and coordinate_item is not None:
            coordinate_form = FirePlanCoordinateReviewForm(
                request.POST,
                longitude=coordinate_item["longitude"],
                latitude=coordinate_item["latitude"],
            )
            if not coordinate_form.is_valid():
                context = _review_region_context(
                    batch,
                    review=review,
                    coordinate_data=request.POST,
                    coordinate_row_index=coordinate_item["index"],
                )
                if request.headers.get("HX-Request") == "true":
                    return render(
                        request,
                        "ingestion/partials/_review_region.html",
                        {"department": department, **context},
                    )
                return render(
                    request,
                    "ingestion/preview.html",
                    {"department": department, **context},
                )
        with transaction.atomic():
            if coordinate_form is not None:
                batch = set_review_coordinates(
                    actor=request.user,
                    batch_id=batch.id,
                    row_index=coordinate_item["index"],
                    longitude=coordinate_form.cleaned_data["longitude"],
                    latitude=coordinate_form.cleaned_data["latitude"],
                )
            batch = set_review_decision(
                actor=request.user, batch_id=batch.id, key=key, decision=decision
            )
    except ImportError as error:
        messages.error(request, str(error))
    if request.headers.get("HX-Request") == "true":
        batch.refresh_from_db()
        return _render_review_region(request, department, batch)
    # Re-enter the wizard without a positional index so ``review_context``
    # selects the next unresolved update deterministically.
    return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)


@login_required
@require_http_methods(["POST"])
def review_approve(request: HttpRequest, department_id, batch_id, key) -> HttpResponse:
    return _review_decide(request, department_id, batch_id, key, "approved")


@login_required
@require_http_methods(["POST"])
def review_skip(request: HttpRequest, department_id, batch_id, key) -> HttpResponse:
    return _review_decide(request, department_id, batch_id, key, "skipped")


@login_required
@require_http_methods(["POST"])
def review_station_resolution(request: HttpRequest, department_id, batch_id, key) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    review = review_context(batch)
    current = review.get("current")
    kind = str(current.get("kind", "")) if isinstance(current, dict) else ""
    if kind not in {"missing", "ambiguous"} or str(current.get("key")) != key:
        messages.error(request, "Station review target is unavailable.")
        return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    context = _review_region_context(batch, review=review, coordinate_data=request.POST)
    form = context["station_resolution_form"]
    if form is None or not form.is_valid():
        if request.headers.get("HX-Request") == "true":
            return render(
                request,
                "ingestion/partials/_review_region.html",
                {"department": department, **context},
            )
        return render(request, "ingestion/preview.html", {"department": department, **context})
    try:
        set_station_vehicle_resolution(
            actor=request.user,
            batch_id=batch.id,
            key=key,
            resolution_kind=kind,
            values=form.cleaned_data,
        )
    except ImportError as error:
        messages.error(request, str(error))
    batch.refresh_from_db()
    if request.headers.get("HX-Request") == "true":
        return _render_review_region(request, department, batch)
    return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)


@login_required
@require_http_methods(["POST"])
def review_personnel_home_station_resolution(
    request: HttpRequest, department_id, batch_id, key
) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    review = review_context(batch)
    current = review.get("current")
    if (
        not isinstance(current, dict)
        or current.get("kind") != "personnel_ambiguous_home_station"
        or str(current.get("key")) != key
    ):
        messages.error(request, "Home Station review target is unavailable.")
        return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    context = _review_region_context(batch, review=review, coordinate_data=request.POST)
    form = context["personnel_resolution_form"]
    if form is None or not form.is_valid():
        if request.headers.get("HX-Request") == "true":
            return render(
                request,
                "ingestion/partials/_review_region.html",
                {"department": department, **context},
            )
        return render(request, "ingestion/preview.html", {"department": department, **context})
    try:
        set_personnel_home_station_resolution(
            actor=request.user,
            batch_id=batch.id,
            key=key,
            station=form.cleaned_data["station_id"],
        )
    except ImportError as error:
        messages.error(request, str(error))
    batch.refresh_from_db()
    if request.headers.get("HX-Request") == "true":
        return _render_review_region(request, department, batch)
    return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)


@login_required
@require_http_methods(["POST"])
def review_coordinates(
    request: HttpRequest, department_id, batch_id, row_index: int
) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    context = _review_region_context(
        batch, coordinate_data=request.POST, coordinate_row_index=row_index
    )
    coordinate_item = context["coordinate_item"]
    if coordinate_item is None:
        messages.error(request, "Coordinate review target is unavailable.")
        return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    coordinate_form = context["coordinate_form"]
    if not coordinate_form.is_valid():
        if request.headers.get("HX-Request") == "true":
            return render(
                request,
                "ingestion/partials/_review_region.html",
                {"department": department, **context},
            )
        return render(request, "ingestion/preview.html", {"department": department, **context})
    try:
        set_review_coordinates(
            actor=request.user,
            batch_id=batch.id,
            row_index=row_index,
            longitude=coordinate_form.cleaned_data["longitude"],
            latitude=coordinate_form.cleaned_data["latitude"],
        )
    except ImportError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Coordinates added to the staged Fire Plan preview.")
    batch.refresh_from_db()
    if request.headers.get("HX-Request") == "true":
        return _render_review_region(request, department, batch)
    return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)


@login_required
@require_http_methods(["POST"])
def review_approve_all(request: HttpRequest, department_id, batch_id) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    if request.POST.get("confirm") != "true":
        messages.error(request, "Approve-all requires explicit confirmation.")
        return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    try:
        approve_all_review_decisions(actor=request.user, batch_id=batch.id)
    except ImportError as error:
        messages.error(request, str(error))
    return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)


@login_required
@require_http_methods(["POST"])
def cancel(request: HttpRequest, department_id, batch_id) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    try:
        cancel_preview(actor=request.user, batch_id=batch.id)
    except ImportError as error:
        messages.error(request, str(error))
        return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    messages.success(request, "Import preview cancelled; no canonical data changed.")
    return redirect(_imports_url(department=department, domain=batch.domain))
