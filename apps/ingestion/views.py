from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.forms import ModelChoiceField
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.authorization.services import require_department_admin
from apps.ingestion.forms import ImportUploadForm
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    approve_all_review_decisions,
    cancel_preview,
    create_preview,
    review_context,
    set_review_decision,
)
from apps.organizations.models import Department, Station


def _department(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    return department


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
                (ImportBatch.Format.JSON, "JSON"),
            ),
            "mode": ImportBatch.Mode.MERGE,
            "help": (
                "GeoJSON is preferred. CSV and JSON are also accepted; coordinates use EPSG:4326 "
                "longitude then latitude."
            ),
        },
        ImportBatch.Domain.PERSONNEL: {
            "formats": ((ImportBatch.Format.CSV, "CSV"), (ImportBatch.Format.JSON, "JSON")),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "CSV and JSON add or update personnel. Absence never offboards people or ends "
                "assignments."
            ),
        },
        ImportBatch.Domain.FIRE_PLANS: {
            "formats": ((ImportBatch.Format.ZIP, "ZIP package"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": (
                "ZIP packages use fire-plans-manifest-v1.csv. An upsert creates a plan when "
                "its External ID/address identity is new, otherwise it updates that plan."
            ),
        },
        ImportBatch.Domain.KLGV_PLANS: {
            "formats": ((ImportBatch.Format.ZIP, "ZIP package"),),
            "mode": ImportBatch.Mode.UPSERT,
            "help": "ZIP packages use manifest.csv and create or update KLGV plans by stable ID.",
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
    return render(
        request,
        "ingestion/imports.html",
        {
            "department": department,
            "form": form,
            "batches": ImportBatch.objects.filter(
                department=department, **({"domain": target_domain} if target_domain else {})
            )[:20],
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
            return redirect("ingestion-imports", department_id=department.id)
    return render(
        request,
        "ingestion/preview.html",
        {"department": department, "batch": batch, "review": _review_context(request, batch)},
    )


def _review_context(request: HttpRequest, batch: ImportBatch) -> dict[str, object] | None:
    if batch.domain not in {
        ImportBatch.Domain.FIRE_PLANS,
        ImportBatch.Domain.KLGV_PLANS,
    }:
        return None
    try:
        index = int(request.GET.get("review", "0"))
    except (TypeError, ValueError):
        index = 0
    return review_context(batch, index)


def _review_decide(request, department_id, batch_id, key, decision) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    try:
        set_review_decision(actor=request.user, batch_id=batch.id, key=key, decision=decision)
    except ImportError as error:
        messages.error(request, str(error))
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
    return redirect("ingestion-imports", department_id=department.id)
