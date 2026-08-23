from datetime import timedelta
from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.audit.services import record_event
from apps.authorization.scopes import (
    StationAdminContextError,
    active_department_ids,
    active_station_ids,
    station_admin_station,
)
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import ImportError, create_single_preview
from apps.organizations.models import Department, Station
from apps.personnel.forms import (
    CommanderEligibilityForm,
    CommanderEmailForm,
    PersonForm,
    PersonnelFilterForm,
    RetentionPolicyForm,
)
from apps.personnel.models import Person
from apps.personnel.services import (
    PersonnelError,
    anonymize_person,
    delete_person,
    offboard_person,
    set_commander_eligibility,
    set_commander_email,
    set_retention_policy,
    update_person,
    verify_commander_email,
    visible_to_user,
)

PERSONNEL_LIST_PAGE_SIZE = 100


def _person_or_404(request: HttpRequest, department_id, person_id) -> Person:
    return cast(
        Person,
        get_object_or_404(
            visible_to_user(user=request.user, department_id=department_id), pk=person_id
        ),
    )


def _station_admin_station_or_403(request: HttpRequest, department: Department) -> Station:
    """Resolve the single authorized station for a station-only administrator.

    A Station Administrator administers exactly one station, so no ``?station=``
    query parameter is required to reach their own data. An inconsistent
    multi-station assignment fails safely instead of silently picking one.
    """
    try:
        station = station_admin_station(request.user)
    except StationAdminContextError as error:
        record_event(
            action="authorization.station_admin_ambiguous_scope",
            request=request,
            actor_user=request.user,
            department=department,
            target_type="station_admin",
        )
        raise PermissionDenied(str(error)) from error
    if station is None or station.department_id != department.id:
        raise PermissionDenied("Station administrator scope does not include this department.")
    return station


@require_http_methods(["GET", "POST"])
@login_required
def people(request: HttpRequest, department_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    department_admin = department.id in active_department_ids(request.user)
    selected_station = None
    if not department_admin:
        selected_station = _station_admin_station_or_403(request, department)
    queryset = visible_to_user(user=request.user, department_id=department.id)
    if selected_station:
        queryset = queryset.filter(station_assignments__station=selected_station).distinct()
    filter_form = PersonnelFilterForm(request.GET or None)
    if filter_form.is_valid():
        filters = filter_form.cleaned_data
        if filters.get("q"):
            queryset = queryset.filter(
                Q(display_name__icontains=filters["q"])
                | Q(personnel_number__icontains=filters["q"])
            )
        if filters.get("status"):
            queryset = queryset.filter(lifecycle_status=filters["status"])
        if filters.get("home_station"):
            queryset = queryset.filter(
                station_assignments__station_id=filters["home_station"],
                station_assignments__assignment_type="HOME",
                station_assignments__ended_at__isnull=True,
                station_assignments__valid_until__isnull=True,
            )
    if "status" not in request.GET:
        queryset = queryset.filter(lifecycle_status=Person.LifecycleStatus.ACTIVE)
    queryset = queryset.order_by("display_name", "personnel_number", "id").distinct()
    paginator = Paginator(queryset, PERSONNEL_LIST_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))
    station_queryset = department.stations.filter(active=True).order_by("name")
    if selected_station:
        station_queryset = station_queryset.filter(pk=selected_station.pk)
    if request.method == "POST":
        if not department_admin:
            return HttpResponse(status=403)
        form = PersonForm(request.POST)
        home_station = get_object_or_404(
            Station,
            pk=request.POST.get("home_station_id"),
            department=department,
            active=True,
        )
        if form.is_valid():
            try:
                batch = create_single_preview(
                    actor=request.user,
                    department=department,
                    domain=ImportBatch.Domain.PERSONNEL,
                    values={
                        **form.cleaned_data,
                        "incident_commander_eligible": False,
                    },
                    station=home_station,
                    original_filename="manual-personnel-v1.csv",
                )
                return redirect("ingestion-preview", department_id=department.id, batch_id=batch.id)
            except (PersonnelError, ImportError) as error:
                form.add_error(None, str(error))
    else:
        form = PersonForm()
    return render(
        request,
        "personnel/list.html",
        {
            "department": department,
            "people": page.object_list,
            "page": page,
            "total_count": paginator.count,
            "filter_form": filter_form,
            "form": form,
            "department_admin": department_admin,
            "stations": station_queryset,
            "station_options": [
                (
                    str(station.id),
                    f"{station.name} ({station.short_code})"
                    + (f", {station.city}" if station.city else ""),
                )
                for station in station_queryset
            ],
            "station_selector_endpoint": reverse(
                "portal-scoped-selector", args=(department.id, "stations")
            ),
            "selected_station": selected_station,
            "recent_batches": ImportBatch.objects.filter(
                department=department, domain=ImportBatch.Domain.PERSONNEL
            ).order_by("-created_at")[:10],
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def person_detail(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    department_admin = person.department_id in active_department_ids(request.user)
    form = PersonForm(
        request.POST or None,
        initial={
            "personnel_number": person.personnel_number,
            "first_name": person.first_name,
            "last_name": person.last_name,
        },
    )
    if request.method == "POST" and not department_admin:
        return HttpResponse(status=403)
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_single_preview(
                actor=request.user,
                department=person.department,
                domain=ImportBatch.Domain.PERSONNEL,
                values={
                    **form.cleaned_data,
                    "incident_commander_eligible": person.incident_commander_eligible,
                },
                original_filename="manual-personnel-v1.csv",
            )
            return redirect("ingestion-preview", department_id=department_id, batch_id=batch.id)
        except (PersonnelError, ImportError) as error:
            form.add_error(None, str(error))
    return render(
        request,
        "personnel/detail.html",
        {"person": person, "form": form, "department_admin": department_admin},
    )


@require_http_methods(["GET", "POST"])
@login_required
def person_edit_modal(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    if person.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator scope is required.")
    form = PersonForm(
        request.POST or None,
        initial={
            "personnel_number": person.personnel_number,
            "first_name": person.first_name,
            "last_name": person.last_name,
        },
    )
    if request.method == "POST" and form.is_valid():
        update_person(actor=request.user, person=person, **form.cleaned_data)
        return redirect("personnel-detail", department_id=person.department_id, person_id=person.id)
    return render(request, "personnel/_person_form_modal.html", {"form": form})


@require_http_methods(["GET", "POST"])
@login_required
def person_delete_modal(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    if person.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator scope is required.")
    if request.method == "POST":
        try:
            delete_person(actor=request.user, person=person)
        except PersonnelError as error:
            return render(
                request,
                "portal/_delete_modal.html",
                {"object": person, "error": str(error), "action_url": request.path},
            )
        return redirect("personnel-list", department_id=person.department_id)
    return render(
        request,
        "portal/_delete_modal.html",
        {"object": person, "action_url": request.path},
    )


@require_http_methods(["POST"])
@login_required
def commander_eligibility(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    form = CommanderEligibilityForm(request.POST)
    if form.is_valid():
        try:
            station = None
            if person.department_id not in active_department_ids(request.user):
                station = get_object_or_404(
                    Station,
                    pk=request.POST.get("station_id"),
                    department_id=department_id,
                    id__in=active_station_ids(request.user),
                )
            set_commander_eligibility(
                actor=request.user,
                person=person,
                eligible=form.cleaned_data["eligible"],
                station=station,
            )
        except PersonnelError as error:
            messages.error(request, str(error))
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
@login_required
def commander_email(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    form = CommanderEmailForm(request.POST)
    if form.is_valid():
        try:
            set_commander_email(actor=request.user, person=person, email=form.cleaned_data["email"])
        except PersonnelError as error:
            messages.error(request, str(error))
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
@login_required
def verify_email(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(
        request,
        return_url=reverse("personnel-detail", args=(department_id, person_id)),
    )
    person = _person_or_404(request, department_id, person_id)
    try:
        verify_commander_email(actor=request.user, person=person)
    except PersonnelError as error:
        messages.error(request, str(error))
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
@login_required
def offboard(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(
        request,
        return_url=reverse("personnel-detail", args=(department_id, person_id)),
    )
    person = _person_or_404(request, department_id, person_id)
    try:
        offboard_person(actor=request.user, person=person)
    except PersonnelError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Personnel record offboarded.")
    return redirect("personnel-list", department_id=department_id)


@require_http_methods(["POST"])
@login_required
def anonymize(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(
        request,
        return_url=reverse("personnel-detail", args=(department_id, person_id)),
    )
    person = _person_or_404(request, department_id, person_id)
    try:
        anonymize_person(actor=request.user, person=person)
    except PersonnelError as error:
        messages.error(request, str(error))
    return redirect("personnel-list", department_id=department_id)


@require_http_methods(["GET", "POST"])
@login_required
def retention_policy(request: HttpRequest, department_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=reverse("personnel-retention-policy", args=(department.id,)),
        )
        form = RetentionPolicyForm(request.POST)
        if form.is_valid():
            set_retention_policy(
                actor=request.user,
                department=department,
                retention_period=timedelta(days=form.cleaned_data["retention_days"]),
            )
            return redirect("personnel-retention-policy", department_id=department.id)
    else:
        policy = getattr(department, "personnel_retention_policy", None)
        form = RetentionPolicyForm(
            initial={"retention_days": policy.retention_period.days if policy else None}
        )
    return render(
        request, "personnel/retention_policy.html", {"department": department, "form": form}
    )
