from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.accounts.reauth import require_recent_reauthentication
from apps.audit.models import AuditEvent
from apps.authorization.models import StationAdminAssignment
from apps.authorization.scopes import active_department_ids, active_station_ids, is_system_admin
from apps.authorization.services import (
    change_department_status,
    create_department,
    grant_station_admin,
    provision_department_admin,
    provision_station_admin,
    revoke_station_admin,
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
    DepartmentTabletLeaseForm,
    RevokeStationScopeForm,
    StationForm,
    StationScopeForm,
    VehicleForm,
)


def _nav_context(request):
    ctx: dict[str, object] = {}
    ctx["is_system_admin"] = is_system_admin(request.user)
    if ctx["is_system_admin"]:
        # System administration is intentionally isolated from customer operations.
        return {"is_system_admin": True, "nav_system": True, "nav_mode": "system"}
    depts = Department.objects.filter(id__in=active_department_ids(request.user))
    ctx["nav_departments"] = depts
    if depts:
        ctx["nav_department"] = depts.order_by("name").first()
    station_ids = active_station_ids(request.user)
    nav_stations = Station.objects.filter(id__in=station_ids)
    ctx["nav_stations"] = nav_stations
    nav_mode = request.GET.get("nav", "")
    if nav_mode == "department" and depts:
        ctx["nav_mode"] = "department"
    elif nav_mode == "station" and station_ids:
        ctx["nav_mode"] = "station"
        station_param = request.GET.get("station")
        if station_param:
            try:
                s = nav_stations.get(pk=station_param)
                ctx["nav_station"] = s
            except Station.DoesNotExist:
                pass
        elif nav_stations.count() == 1:
            ctx["nav_station"] = nav_stations.first()
    elif depts and station_ids:
        ctx["nav_mode"] = "department"
    elif depts:
        ctx["nav_mode"] = "department"
    elif station_ids:
        ctx["nav_mode"] = "station"
        if nav_stations.count() == 1:
            ctx["nav_station"] = nav_stations.first()
    return ctx


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    if department.id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator scope is required.")
    return department


def _station_or_403(request: HttpRequest, station_id) -> Station:
    station = get_object_or_404(Station, pk=station_id)
    has_station = station.id in active_station_ids(request.user)
    has_dept = station.department_id in active_department_ids(request.user)
    if not has_station and not has_dept:
        raise PermissionDenied("Station scope or department administrator scope is required.")
    return station


@login_required
@require_http_methods(["GET"])
def scoped_selector(request: HttpRequest, department_id, kind: str) -> HttpResponse:
    """Return scoped UUID options for progressively enhanced relationship selects."""
    department = _department_or_403(request, department_id)
    query = request.GET.get("q", "").strip()
    if kind == "stations":
        stations = department.stations.filter(active=True)
        if query:
            stations = stations.filter(Q(name__icontains=query) | Q(short_code__icontains=query))
        options = [
            (
                str(item.id),
                f"{item.name} ({item.short_code})" + (f", {item.city}" if item.city else ""),
            )
            for item in stations.order_by("name")[:25]
        ]
    elif kind == "personnel":
        from apps.personnel.models import Person

        personnel = Person.objects.filter(department=department, active=True)
        if query:
            personnel = personnel.filter(
                Q(display_name__icontains=query) | Q(personnel_number__icontains=query)
            )
        options = [
            (str(item.id), item.display_name) for item in personnel.order_by("display_name")[:25]
        ]
    elif kind == "vehicles":
        vehicles = Vehicle.objects.filter(department=department, active=True)
        if query:
            vehicles = vehicles.filter(
                Q(display_name__icontains=query) | Q(call_sign__icontains=query)
            )
        options = [
            (str(item.id), item.display_name) for item in vehicles.order_by("display_name")[:25]
        ]
    elif kind == "departments" and is_system_admin(request.user):
        departments = Department.objects.all()
        if query:
            departments = departments.filter(
                Q(name__icontains=query) | Q(short_code__icontains=query)
            )
        options = [
            (str(item.id), f"{item.name} ({item.short_code})")
            for item in departments.order_by("name")[:25]
        ]
    else:
        raise PermissionDenied("A supported scoped selector is required.")
    return render(request, "components/_selector_options.html", {"options": options})


@login_required
@never_cache
def dashboard(request: HttpRequest) -> HttpResponse:
    ctx = _nav_context(request)
    # System-only navigation intentionally has no client-operation querysets.
    ctx["departments"] = ctx.pop("nav_departments", Department.objects.none())
    ctx["stations"] = ctx.pop("nav_stations", Station.objects.none())
    return render(request, "portal/dashboard.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def system_departments(request: HttpRequest) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    form = DepartmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request, return_url=reverse("portal-system-departments"))
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
    lease_form = DepartmentTabletLeaseForm(
        initial={"tablet_lease_days": department.tablet_lease_days}
    )
    admin_form = AdministratorForm()
    if (
        request.method == "POST"
        and request.POST.get("action") == "status"
        and status_form.is_valid()
    ):
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-system-department", args=(department.id,)),
        )
        change_department_status(
            actor=request.user, department=department, status=status_form.cleaned_data["status"]
        )
        return redirect("portal-system-department", department_id=department.id)
    if request.method == "POST" and request.POST.get("action") == "provision":
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-system-department", args=(department.id,)),
        )
        admin_form = AdministratorForm(request.POST)
        if admin_form.is_valid():
            token = provision_department_admin(
                actor=request.user, department=department, **admin_form.cleaned_data
            )
    if request.method == "POST" and request.POST.get("action") == "tablet-lease":
        lease_form = DepartmentTabletLeaseForm(request.POST)
        if lease_form.is_valid():
            require_recent_reauthentication(
                request,
                return_url=reverse("portal-system-department", args=(department.id,)),
            )
            department.tablet_lease_days = lease_form.cleaned_data["tablet_lease_days"]
            department.save(update_fields=("tablet_lease_days",))
            return redirect("portal-system-department", department_id=department.id)
            return render(
                request,
                "portal/setup_link.html",
                {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
            )
    return render(
        request,
        "portal/system_department_detail.html",
        {
            "department": department,
            "status_form": status_form,
            "admin_form": admin_form,
            "lease_form": lease_form,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def department_manage(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = AdministratorForm(request.POST or None)
    scope_form = StationScopeForm(request.POST or None)
    revoke_scope_form = RevokeStationScopeForm(request.POST or None)
    action = request.POST.get("action")
    if request.method == "POST" and action == "provision" and form.is_valid():
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-department-manage", args=(department.id,)),
        )
        token = provision_department_admin(
            actor=request.user, department=department, **form.cleaned_data
        )
        return render(
            request,
            "portal/setup_link.html",
            {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
        )
    administrators = User.objects.filter(
        Q(department_memberships__department=department, department_memberships__active=True)
        | Q(station_admin_assignments__station__department=department)
    ).distinct()
    query = request.GET.get("q", "").strip()
    station_filter = request.GET.get("station", "")
    if query:
        administrators = administrators.filter(
            Q(display_name__icontains=query) | Q(email__icontains=query)
        )
    if station_filter:
        administrators = administrators.filter(
            station_admin_assignments__station_id=station_filter,
            station_admin_assignments__active=True,
        )
    if request.method == "POST" and action == "grant-station" and scope_form.is_valid():
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-department-manage", args=(department.id,)),
        )
        user = get_object_or_404(administrators, pk=scope_form.cleaned_data["user_id"])
        station = get_object_or_404(
            Station, pk=scope_form.cleaned_data["station_id"], department=department, active=True
        )
        grant_station_admin(actor=request.user, user=user, station=station)
        return redirect("portal-department-manage", department_id=department.id)
    if request.method == "POST" and action == "revoke-station" and revoke_scope_form.is_valid():
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-department-manage", args=(department.id,)),
        )
        assignment = get_object_or_404(
            StationAdminAssignment,
            pk=revoke_scope_form.cleaned_data["assignment_id"],
            station__department=department,
            active=True,
        )
        revoke_station_admin(actor=request.user, assignment=assignment)
        return redirect("portal-department-manage", department_id=department.id)
    administrators = administrators.prefetch_related(
        "department_memberships", "station_admin_assignments__station"
    ).order_by("email")
    current_user_id = request.user.id
    return render(
        request,
        "portal/department_manage.html",
        {
            "department": department,
            "form": form,
            "administrators": administrators,
            "stations": department.stations.filter(active=True).order_by("name"),
            "query": query,
            "station_filter": station_filter,
            "current_user_id": current_user_id,
        },
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
            "street": station.street,
            "house_number": station.house_number,
            "postal_code": station.postal_code,
            "city": station.city,
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
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-station-manage", args=(station.id,)),
        )
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
    department_admin = vehicle.department_id in active_department_ids(request.user)
    station_admin = vehicle.station_id in active_station_ids(request.user)
    if not department_admin and not station_admin:
        raise PermissionDenied("Station scope or department administrator scope is required.")
    if not department_admin:
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


@login_required
@require_http_methods(["GET"])
def department_audit(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    station_id = request.GET.get("station")
    events = AuditEvent.objects.filter(department=department).select_related("actor_user")
    if station_id and station_id != "__all__":
        station = get_object_or_404(Station, pk=station_id, department=department)
        events = events.filter(station=station)
    events = events.order_by("-timestamp")[:100]
    stations = Station.objects.filter(department=department)
    return render(
        request,
        "portal/department_audit.html",
        {
            "department": department,
            "events": events,
            "stations": stations,
            "selected_station": station_id or "__all__",
        },
    )
