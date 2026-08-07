from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.authorization.scopes import active_department_ids, active_station_ids, is_system_admin
from apps.authorization.services import (
    change_department_status,
    create_department,
    provision_department_admin,
    provision_station_admin,
)
from apps.organizations.models import Department, Station, Vehicle
from apps.organizations.services import (
    create_station,
    create_vehicle,
    update_station,
    update_vehicle,
)
from apps.portal.forms import (
    AdministratorForm,
    DepartmentForm,
    DepartmentStatusForm,
    StationForm,
    VehicleForm,
)


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    if department.id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator scope is required.")
    return department


def _station_or_403(request: HttpRequest, station_id) -> Station:
    station = get_object_or_404(Station, pk=station_id)
    if station.id not in active_station_ids(request.user):
        raise PermissionDenied("Station scope is required.")
    return station


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    departments = Department.objects.filter(id__in=active_department_ids(request.user))
    stations = Station.objects.filter(id__in=active_station_ids(request.user))
    return render(
        request,
        "portal/dashboard.html",
        {
            "is_system_admin": is_system_admin(request.user),
            "departments": departments,
            "stations": stations,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def system_departments(request: HttpRequest) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    form = DepartmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request)
        create_department(actor=request.user, **form.cleaned_data)
        return redirect("portal-system-departments")
    return render(
        request,
        "portal/system_departments.html",
        {"departments": Department.objects.all(), "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def system_department_detail(request: HttpRequest, department_id) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    department = get_object_or_404(Department, pk=department_id)
    status_form = DepartmentStatusForm(request.POST or None, initial={"status": department.status})
    admin_form = AdministratorForm()
    if (
        request.method == "POST"
        and request.POST.get("action") == "status"
        and status_form.is_valid()
    ):
        require_recent_reauthentication(request)
        change_department_status(
            actor=request.user, department=department, status=status_form.cleaned_data["status"]
        )
        return redirect("portal-system-department", department_id=department.id)
    if request.method == "POST" and request.POST.get("action") == "provision":
        require_recent_reauthentication(request)
        admin_form = AdministratorForm(request.POST)
        if admin_form.is_valid():
            token = provision_department_admin(
                actor=request.user, department=department, **admin_form.cleaned_data
            )
            return render(
                request,
                "portal/setup_link.html",
                {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
            )
    return render(
        request,
        "portal/system_department_detail.html",
        {"department": department, "status_form": status_form, "admin_form": admin_form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def department_manage(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = AdministratorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request)
        token = provision_department_admin(
            actor=request.user, department=department, **form.cleaned_data
        )
        return render(
            request,
            "portal/setup_link.html",
            {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
        )
    memberships = DepartmentMembership.objects.filter(department=department, active=True)
    return render(
        request,
        "portal/department_manage.html",
        {"department": department, "form": form, "memberships": memberships},
    )


@login_required
@require_http_methods(["GET", "POST"])
def stations(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = StationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_station(actor=request.user, department=department, **form.cleaned_data)
        return redirect("portal-stations", department_id=department.id)
    return render(
        request,
        "portal/stations.html",
        {"department": department, "stations": department.stations.all(), "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def station_manage(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    department_admin = station.department_id in active_department_ids(request.user)
    form = StationForm(
        request.POST or None,
        initial={
            "name": station.name,
            "short_code": station.short_code,
            "address": station.address,
            "active": station.active,
        },
    )
    admin_form = AdministratorForm()
    if request.method == "POST" and not department_admin:
        raise PermissionDenied("Department administrator role is required.")
    if request.method == "POST" and request.POST.get("action") == "station" and form.is_valid():
        update_station(actor=request.user, station=station, **form.cleaned_data)
        return redirect("portal-station-manage", station_id=station.id)
    if request.method == "POST" and request.POST.get("action") == "provision":
        require_recent_reauthentication(request)
        admin_form = AdministratorForm(request.POST)
        if admin_form.is_valid():
            token = provision_station_admin(
                actor=request.user, station=station, **admin_form.cleaned_data
            )
            return render(
                request,
                "portal/setup_link.html",
                {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
            )
    return render(
        request,
        "portal/station_manage.html",
        {
            "station": station,
            "department_admin": department_admin,
            "form": form,
            "admin_form": admin_form,
            "assignments": StationAdminAssignment.objects.filter(station=station, active=True),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicles(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    department_admin = station.department_id in active_department_ids(request.user)
    form = VehicleForm(request.POST or None)
    if request.method == "POST":
        if not department_admin:
            raise PermissionDenied("Department administrator role is required.")
        if form.is_valid():
            create_vehicle(
                actor=request.user,
                department=station.department,
                station=station,
                **form.cleaned_data,
            )
            return redirect("portal-vehicles", station_id=station.id)
    return render(
        request,
        "portal/vehicles.html",
        {
            "station": station,
            "vehicles": station.vehicles.all(),
            "form": form if department_admin else None,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicle_manage(request: HttpRequest, vehicle_id) -> HttpResponse:
    vehicle = get_object_or_404(Vehicle, pk=vehicle_id)
    if vehicle.station_id not in active_station_ids(request.user):
        raise PermissionDenied("Station scope is required.")
    if vehicle.department_id not in active_department_ids(request.user):
        if request.method == "POST":
            raise PermissionDenied("Department administrator role is required.")
        return render(request, "portal/vehicle_manage.html", {"vehicle": vehicle, "form": None})
    form = VehicleForm(
        request.POST or None,
        initial={
            "display_name": vehicle.display_name,
            "call_sign": vehicle.call_sign,
            "asset_identifier": vehicle.asset_identifier,
            "active": vehicle.active,
        },
    )
    if request.method == "POST" and form.is_valid():
        update_vehicle(actor=request.user, vehicle=vehicle, **form.cleaned_data)
        return redirect("portal-vehicle-manage", vehicle_id=vehicle.id)
    return render(request, "portal/vehicle_manage.html", {"vehicle": vehicle, "form": form})
