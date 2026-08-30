from typing import cast
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
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
    FirePlanFilterForm,
    FirePlanUploadForm,
    HydrantEditForm,
    HydrantFilterForm,
    HydrantForm,
    KlgvPlanEditForm,
    KlgvPlanUploadForm,
    PhonebookEntryForm,
    PhonebookFilterForm,
)
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan, PhonebookEntry
from apps.reference_data.phonebook import (
    find_duplicate_candidates,
    find_entry_duplicate_candidates,
    normalize_phone_number,
)
from apps.reference_data.services import (
    create_phonebook_entry,
    delete_fire_plan,
    delete_hydrant,
    delete_klgv_plan,
    delete_phonebook_entry,
    require_current_phonebook_reconciliation_candidate,
    resolve_phonebook_duplicate,
    set_fire_plan_active,
    set_klgv_plan_active,
    update_fire_plan,
    update_hydrant,
    update_klgv_plan,
    update_phonebook_entry,
)

HYDRANT_LIST_PAGE_SIZE = 100
DOCUMENT_LIST_PAGE_SIZE = 100
PHONEBOOK_LIST_PAGE_SIZE = 100
PHONEBOOK_ENTRY_FIELDS = (
    "station",
    "first_name",
    "last_name",
    "organization_unit",
    "function",
    "phone_number",
)
PHONEBOOK_MANUAL_REVIEW_SESSION_KEY = "phonebook_manual_duplicate_review"


def _configure_live_list_filters(form, *, request: HttpRequest, target: str, form_id: str) -> None:
    """Put the established input-event HTMX contract on rendered filter controls."""
    for name, field in form.fields.items():
        if name == "q":
            field.widget.input_type = "search"
        field.widget.attrs.update(
            {
                "hx-get": request.path,
                "hx-trigger": "input changed delay:1s"
                if name == "q"
                else "change",
                "hx-target": target,
                "hx-swap": "outerHTML",
                "hx-include": f"#{form_id}",
                "hx-push-url": "true",
            }
        )


def _phonebook_entry_or_404(request: HttpRequest, entry_id) -> PhonebookEntry:
    entry = get_object_or_404(
        PhonebookEntry.objects.select_related("department", "station"),
        pk=entry_id,
        department_id__in=active_department_ids(request.user),
    )
    require_department_admin(request.user, entry.department)
    return entry


@login_required
@require_http_methods(["GET"])
def phonebook(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    context = _phonebook_list_context(request=request, department=department)
    return render(
        request,
        "reference_data/_phonebook_results.html"
        if request.headers.get("HX-Request") == "true"
        else "reference_data/phonebook.html",
        context,
    )


def _phonebook_list_context(*, request: HttpRequest, department: Department) -> dict[str, object]:
    form = PhonebookFilterForm(request.GET or None, department=department)
    _configure_live_list_filters(
        form, request=request, target="#phonebook-results", form_id="phonebook-filter-form"
    )
    entries = (
        PhonebookEntry.objects.filter(department=department)
        .select_related("station")
        .order_by("last_name", "first_name", "organization_unit", "id")
    )
    if form.is_valid():
        data = form.cleaned_data
        if data.get("q"):
            entries = entries.filter(
                Q(first_name__icontains=data["q"])
                | Q(last_name__icontains=data["q"])
                | Q(organization_unit__icontains=data["q"])
                | Q(function__icontains=data["q"])
                | Q(phone_number__icontains=data["q"])
            )
        if data.get("scope") == "department":
            entries = entries.filter(station__isnull=True)
        elif data.get("scope") == "station":
            entries = entries.filter(station__isnull=False)
        if data.get("station"):
            entries = entries.filter(station=data["station"])
    paginator = Paginator(entries, PHONEBOOK_LIST_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = urlencode(
        [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
    )
    return {
        "department": department,
        "form": form,
        "entries": page.object_list,
        "page": page,
        "total_count": paginator.count,
        "page_query": page_query,
        "phonebook_list_path": reverse("reference-data-phonebook", args=[department.id]),
    }


@login_required
@require_http_methods(["GET", "POST"])
def phonebook_create(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = PhonebookEntryForm(request.POST or None, department=department)
    if request.method == "POST" and form.is_valid():
        values = _phonebook_form_values(form)
        proposed = _phonebook_proposed_entry(department=department, values=values)
        candidates = find_entry_duplicate_candidates(entry=proposed, department=department)
        if candidates:
            _store_phonebook_manual_review(request=request, department=department, values=values)
            return _modal_redirect(
                request,
                reverse("reference-data-phonebook-create-review", args=[department.id]),
            )
        create_phonebook_entry(actor=request.user, department=department, **values)
        return _phonebook_create_success(request=request, department=department)
    return _modal(
        request,
        "reference_data/_phonebook_form_modal.html",
        {
            "form": form,
            "title": "Add phonebook entry",
            "modal_container_id": "phonebook-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def phonebook_create_duplicate_review(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    try:
        values, proposed, candidates, candidate_index = _phonebook_manual_review_state(
            request=request, department=department
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("reference-data-phonebook", department_id=department.id)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "cancel":
            _clear_phonebook_manual_review(request)
            messages.info(request, "Phonebook duplicate review cancelled.")
            return redirect("reference-data-phonebook", department_id=department.id)
        try:
            candidate = _selected_phonebook_candidate(
                request=request, candidates=candidates, candidate_index=candidate_index
            )
            if action == "next":
                if candidate_index + 1 >= len(candidates):
                    raise ValueError("No further duplicate candidate is available.")
                review = request.session[PHONEBOOK_MANUAL_REVIEW_SESSION_KEY]
                review["candidate_index"] = candidate_index + 1
                request.session[PHONEBOOK_MANUAL_REVIEW_SESSION_KEY] = review
                request.session.modified = True
                return redirect(
                    "reference-data-phonebook-create-review", department_id=department.id
                )
            with transaction.atomic():
                entry = require_current_phonebook_reconciliation_candidate(
                    actor=request.user,
                    department=department,
                    entry_id=candidate.second.id,
                    fingerprint=candidate.second_fingerprint,
                )
                if action == "update":
                    update_phonebook_entry(actor=request.user, entry=entry, **values)
                    message = "Phonebook entry updated after duplicate review."
                elif action == "create":
                    create_phonebook_entry(actor=request.user, department=department, **values)
                    message = "Phonebook entry added as a distinct contact."
                else:
                    raise ValueError("Choose a Phonebook reconciliation action.")
        except ValueError as error:
            return _render_phonebook_manual_review(
                request=request,
                department=department,
                proposed=proposed,
                candidates=candidates,
                candidate_index=candidate_index,
                error=str(error),
                status=409,
            )
        _clear_phonebook_manual_review(request)
        messages.success(request, message)
        return redirect("reference-data-phonebook", department_id=department.id)
    return _render_phonebook_manual_review(
        request=request,
        department=department,
        proposed=proposed,
        candidates=candidates,
        candidate_index=candidate_index,
    )


@login_required
@require_http_methods(["GET"])
def phonebook_detail(request: HttpRequest, entry_id) -> HttpResponse:
    return render(
        request,
        "reference_data/phonebook_detail.html",
        {"entry": _phonebook_entry_or_404(request, entry_id)},
    )


@login_required
@require_http_methods(["GET", "POST"])
def phonebook_edit_modal(request: HttpRequest, entry_id) -> HttpResponse:
    entry = _phonebook_entry_or_404(request, entry_id)
    form = PhonebookEntryForm(request.POST or None, instance=entry, department=entry.department)
    if request.method == "POST" and form.is_valid():
        update_phonebook_entry(actor=request.user, entry=entry, **form.cleaned_data)
        return _modal_redirect(request, reverse("reference-data-phonebook-detail", args=[entry.id]))
    return _modal(
        request,
        "reference_data/_phonebook_form_modal.html",
        {
            "form": form,
            "title": "Edit phonebook entry",
            "modal_container_id": "phonebook-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def phonebook_delete_modal(request: HttpRequest, entry_id) -> HttpResponse:
    entry = _phonebook_entry_or_404(request, entry_id)
    if request.method == "POST":
        delete_phonebook_entry(actor=request.user, entry=entry)
        return _modal_redirect(
            request, reverse("reference-data-phonebook", args=[entry.department_id])
        )
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": entry,
            "action_url": request.path,
            "modal_container_id": "phonebook-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def phonebook_duplicate_review(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    if request.method == "POST":
        try:
            resolve_phonebook_duplicate(
                actor=request.user,
                department=department,
                first_id=request.POST["first_id"],
                second_id=request.POST["second_id"],
                first_fingerprint=request.POST["first_fingerprint"],
                second_fingerprint=request.POST["second_fingerprint"],
                action=request.POST["action"],
            )
        except (KeyError, ValueError) as error:
            candidates = find_duplicate_candidates(department=department)
            return render(
                request,
                "reference_data/phonebook_duplicate_review.html",
                {
                    "department": department,
                    "candidate": candidates[0] if candidates else None,
                    "error": str(error),
                },
                status=409,
            )
        return redirect("reference-data-phonebook-duplicates", department_id=department.id)
    candidates = find_duplicate_candidates(department=department)
    return render(
        request,
        "reference_data/phonebook_duplicate_review.html",
        {"department": department, "candidate": candidates[0] if candidates else None},
    )


def _modal(request: HttpRequest, template: str, context: dict[str, object]) -> HttpResponse:
    return render(request, template, context)


def _modal_redirect(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _phonebook_form_values(form: PhonebookEntryForm) -> dict[str, object]:
    values = {field: form.cleaned_data[field] for field in PHONEBOOK_ENTRY_FIELDS}
    values["phone_number"] = normalize_phone_number(str(values["phone_number"]))
    return values


def _phonebook_proposed_entry(
    *, department: Department, values: dict[str, object]
) -> PhonebookEntry:
    return PhonebookEntry(department=department, **values)


def _selected_phonebook_candidate(*, request: HttpRequest, candidates, candidate_index: int):
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise ValueError("Selected Phonebook entry is no longer available.")
    candidate = candidates[candidate_index]
    if (
        request.POST.get("candidate_id") != str(candidate.second.id)
        or request.POST.get("candidate_fingerprint") != candidate.second_fingerprint
    ):
        raise ValueError("Selected Phonebook entry changed; review it again.")
    return candidate


def _store_phonebook_manual_review(*, request: HttpRequest, department: Department, values) -> None:
    request.session[PHONEBOOK_MANUAL_REVIEW_SESSION_KEY] = {
        "actor_id": str(request.user.id),
        "department_id": str(department.id),
        "candidate_index": 0,
        "values": {
            **{field: values[field] for field in PHONEBOOK_ENTRY_FIELDS if field != "station"},
            "station": str(values["station"].id) if values["station"] else None,
        },
    }
    request.session.modified = True


def _phonebook_manual_review_state(*, request: HttpRequest, department: Department):
    workflow = request.session.get(PHONEBOOK_MANUAL_REVIEW_SESSION_KEY)
    if not isinstance(workflow, dict) or (
        workflow.get("actor_id") != str(request.user.id)
        or workflow.get("department_id") != str(department.id)
    ):
        raise ValueError("Phonebook duplicate review is no longer available.")
    stored = workflow.get("values")
    if not isinstance(stored, dict):
        raise ValueError("Phonebook duplicate review is no longer available.")
    station_id = stored.get("station")
    station = None
    if station_id is not None:
        station = department.stations.filter(pk=station_id).first()
        if station is None:
            raise ValueError("The selected Phonebook station is no longer available.")
    values = {
        field: stored.get(field, "")
        for field in PHONEBOOK_ENTRY_FIELDS
        if field != "station"
    }
    values["station"] = station
    proposed = _phonebook_proposed_entry(department=department, values=values)
    proposed.full_clean()
    candidates = find_entry_duplicate_candidates(entry=proposed, department=department)
    if not candidates:
        raise ValueError("Phonebook duplicate candidates changed; submit the entry again.")
    try:
        candidate_index = int(workflow.get("candidate_index", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("Phonebook duplicate review is no longer available.") from error
    if candidate_index < 0 or candidate_index >= len(candidates):
        candidate_index = 0
    return values, proposed, candidates, candidate_index


def _clear_phonebook_manual_review(request: HttpRequest) -> None:
    request.session.pop(PHONEBOOK_MANUAL_REVIEW_SESSION_KEY, None)
    request.session.modified = True


def _render_phonebook_manual_review(
    *,
    request: HttpRequest,
    department: Department,
    proposed,
    candidates,
    candidate_index: int,
    error=None,
    status=200,
) -> HttpResponse:
    return render(
        request,
        "reference_data/phonebook_create_duplicate_review.html",
        {
            "department": department,
            "proposed": proposed,
            "candidate": candidates[candidate_index],
            "candidate_index": candidate_index,
            "candidate_total": len(candidates),
            "has_next": candidate_index + 1 < len(candidates),
            "differences": [
                {
                    "label": label,
                    "proposed": _phonebook_comparison_display(proposed, label),
                    "existing": _phonebook_comparison_display(
                        candidates[candidate_index].second, label
                    ),
                }
                for label in candidates[candidate_index].conflicts
            ],
            "error": error,
        },
        status=status,
    )


def _phonebook_comparison_display(entry: PhonebookEntry, label: str) -> str:
    values = {
        "Name": entry.display_name,
        "Organization unit": entry.organization_unit,
        "Function": entry.function,
        "Phone number": entry.phone_number,
        "Scope": entry.scope_label,
    }
    return values[label] or "—"


def _phonebook_create_success(*, request: HttpRequest, department: Department) -> HttpResponse:
    message = "Phonebook entry added."
    if request.headers.get("HX-Request") != "true":
        messages.success(request, message)
        return redirect("reference-data-phonebook", department_id=department.id)
    response = render(
        request,
        "reference_data/_phonebook_create_success.html",
        _phonebook_list_context(request=request, department=department) | {"message": message},
    )
    response["HX-Trigger"] = '{"phonebook-modal-close": {}}'
    return response


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
    _configure_live_list_filters(
        form, request=request, target="#hydrant-results", form_id="hydrant-filter-form"
    )
    queryset = Hydrant.objects.filter(department=department).order_by("external_identifier", "id")
    if form.is_valid():
        data = form.cleaned_data
        if data.get("q"):
            queryset = queryset.filter(external_identifier__icontains=data["q"])
        if data.get("status"):
            queryset = queryset.filter(status=data["status"])
        if data.get("hydrant_type"):
            queryset = queryset.filter(hydrant_type__icontains=data["hydrant_type"])
        if data.get("street"):
            queryset = queryset.filter(
                Q(street__icontains=data["street"]) | Q(house_number__icontains=data["street"])
            )
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
        "reference_data/_hydrant_results.html"
        if request.headers.get("HX-Request") == "true"
        else "reference_data/hydrants.html",
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
            "longitude": hydrant.geometry.x,
            "latitude": hydrant.geometry.y,
            "street": hydrant.street,
            "house_number": hydrant.house_number,
            "location": hydrant.location,
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
    filters = FirePlanFilterForm(request.GET or None)
    _configure_live_list_filters(
        filters, request=request, target="#fire-plan-results", form_id="fire-plan-filter-form"
    )
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
        if filters.cleaned_data["location_data"] == "complete":
            queryset = queryset.filter(location__isnull=False)
        elif filters.cleaned_data["location_data"] == "missing":
            queryset = queryset.filter(location__isnull=True)
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
        "reference_data/_fire_plan_results.html"
        if request.headers.get("HX-Request") == "true"
        else "reference_data/fire_plans.html",
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
                    "external_identifier": data["external_identifier"],
                    "object_name": data["object_name"],
                    "address": data["address"],
                    "postal_code": data["postal_code"],
                    "city": data["city"],
                },
                pdf_bytes=document.read(),
                original_filename=document.name,
            )
        except ImportError:
            form.add_error("document", "KLGV plan was rejected.")
        else:
            return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
    filters = DocumentFilterForm(request.GET or None)
    _configure_live_list_filters(
        filters, request=request, target="#klgv-plan-results", form_id="klgv-filter-form"
    )
    queryset = KlgvPlan.objects.filter(department=department)
    if filters.is_valid():
        if filters.cleaned_data["q"]:
            query = filters.cleaned_data["q"]
            queryset = queryset.filter(
                Q(external_identifier__icontains=query)
                | Q(object_name__icontains=query)
                | Q(address__icontains=query)
            )
        if filters.cleaned_data["active"]:
            queryset = queryset.filter(active=filters.cleaned_data["active"] == "active")
    if "active" not in request.GET:
        queryset = queryset.filter(active=True)
    paginator = Paginator(
        queryset.order_by("object_name", "external_identifier", "id"), DOCUMENT_LIST_PAGE_SIZE
    )
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = urlencode(
        [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
    )
    return render(
        request,
        "reference_data/_klgv_plan_results.html"
        if request.headers.get("HX-Request") == "true"
        else "reference_data/klgv_plans.html",
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
