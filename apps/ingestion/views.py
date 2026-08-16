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
from apps.ingestion.services import ImportError, apply_preview, cancel_preview, create_preview
from apps.organizations.models import Department, Station


def _department(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    require_department_admin(request.user, department)
    return department


@login_required
@require_http_methods(["GET", "POST"])
def imports(request: HttpRequest, department_id) -> HttpResponse:
    department = _department(request, department_id)
    initial = {
        key: request.GET[key]
        for key in ("domain", "import_format", "import_mode")
        if key in request.GET
    }
    form = ImportUploadForm(request.POST or None, request.FILES or None, initial=initial)
    station_field = cast(ModelChoiceField[Station], form.fields["station"])
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
            "batches": ImportBatch.objects.filter(department=department)[:20],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def preview(request: HttpRequest, department_id, batch_id) -> HttpResponse:
    department = _department(request, department_id)
    batch = get_object_or_404(ImportBatch, pk=batch_id, department=department)
    if request.method == "POST":
        try:
            apply_preview(actor=request.user, batch_id=batch.id)
        except ImportError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Import applied; publication is scheduled separately.")
            return redirect("ingestion-imports", department_id=department.id)
    return render(request, "ingestion/preview.html", {"department": department, "batch": batch})


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
