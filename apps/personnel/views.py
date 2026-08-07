from datetime import timedelta
from typing import cast

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.organizations.models import Department, Station
from apps.personnel.forms import (
    CommanderEligibilityForm,
    CommanderEmailForm,
    PersonForm,
    RetentionPolicyForm,
)
from apps.personnel.models import Person
from apps.personnel.services import (
    PersonnelError,
    anonymize_person,
    create_person,
    offboard_person,
    set_commander_eligibility,
    set_commander_email,
    set_retention_policy,
    update_person,
    verify_commander_email,
    visible_to_user,
)


def _person_or_404(request: HttpRequest, department_id, person_id) -> Person:
    return cast(
        Person,
        get_object_or_404(
            visible_to_user(user=request.user, department_id=department_id), pk=person_id
        ),
    )


@require_http_methods(["GET", "POST"])
def people(request: HttpRequest, department_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    queryset = visible_to_user(user=request.user, department_id=department.id)
    if request.method == "POST":
        form = PersonForm(request.POST)
        home_station = get_object_or_404(Station, pk=request.POST.get("home_station_id"))
        if form.is_valid():
            try:
                create_person(
                    actor=request.user,
                    department=department,
                    home_station=home_station,
                    **form.cleaned_data,
                )
                return redirect("personnel-list", department_id=department.id)
            except PersonnelError as error:
                form.add_error(None, str(error))
    else:
        form = PersonForm()
    return render(
        request, "personnel/list.html", {"department": department, "people": queryset, "form": form}
    )


@require_http_methods(["GET", "POST"])
def person_detail(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    form = PersonForm(
        request.POST or None,
        initial={
            "personnel_number": person.personnel_number,
            "first_name": person.first_name,
            "last_name": person.last_name,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_person(actor=request.user, person=person, **form.cleaned_data)
            return redirect("personnel-detail", department_id=department_id, person_id=person.id)
        except PersonnelError as error:
            form.add_error(None, str(error))
    return render(request, "personnel/detail.html", {"person": person, "form": form})


@require_http_methods(["POST"])
def commander_eligibility(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    form = CommanderEligibilityForm(request.POST)
    if form.is_valid():
        set_commander_eligibility(
            actor=request.user, person=person, eligible=form.cleaned_data["eligible"]
        )
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
def commander_email(request: HttpRequest, department_id, person_id) -> HttpResponse:
    person = _person_or_404(request, department_id, person_id)
    form = CommanderEmailForm(request.POST)
    if form.is_valid():
        set_commander_email(actor=request.user, person=person, email=form.cleaned_data["email"])
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
def verify_email(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(request)
    person = _person_or_404(request, department_id, person_id)
    verify_commander_email(actor=request.user, person=person)
    return redirect("personnel-detail", department_id=department_id, person_id=person.id)


@require_http_methods(["POST"])
def offboard(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(request)
    person = _person_or_404(request, department_id, person_id)
    offboard_person(actor=request.user, person=person)
    messages.success(request, "Personnel record offboarded.")
    return redirect("personnel-list", department_id=department_id)


@require_http_methods(["POST"])
def anonymize(request: HttpRequest, department_id, person_id) -> HttpResponse:
    require_recent_reauthentication(request)
    person = _person_or_404(request, department_id, person_id)
    anonymize_person(actor=request.user, person=person)
    return redirect("personnel-list", department_id=department_id)


@require_http_methods(["GET", "POST"])
def retention_policy(request: HttpRequest, department_id) -> HttpResponse:
    department = get_object_or_404(Department, pk=department_id)
    if request.method == "POST":
        require_recent_reauthentication(request)
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
