from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.accounts.reauth import require_recent_reauthentication
from apps.assignments.services import AssignmentError
from apps.audit.models import AuditEvent
from apps.authorization.models import (
    ApiVersionCompatibilityPolicy,
    DepartmentMembership,
    StationAdminAssignment,
)
from apps.authorization.scopes import (
    StationAdminContextError,
    active_department_ids,
    active_station_ids,
    is_system_admin,
    station_admin_station,
)
from apps.authorization.services import (
    change_department_status,
    create_department,
    grant_station_admin,
    permanently_remove_administrator,
    provision_department_admin,
    provision_station_admin,
    reinstate_department_admin,
    reinstate_station_admin,
    revoke_department_admin,
    revoke_station_admin,
    set_api_version_compatibility_policy,
    set_department_tablet_lease,
    set_system_department_tablet_lease,
    suspend_department_admin,
    suspend_station_admin,
)
from apps.organizations.models import Department, Station, Vehicle
from apps.organizations.services import (
    create_station,
    create_vehicle,
    deactivate_vehicle,
    delete_station,
    delete_vehicle,
    update_station,
    update_vehicle,
)
from apps.personnel.services import set_retention_policy
from apps.portal.forms import (
    AdministratorForm,
    AdministratorRemovalForm,
    ApiVersionCompatibilityPolicyForm,
    DepartmentForm,
    DepartmentStatusForm,
    DepartmentSystemSettingsForm,
    DepartmentTabletLeaseForm,
    StationForm,
    StationListFilterForm,
    VehicleForm,
)
from apps.portal.overview import attention_for_request, system_attention


def _mark_active(sections: list[dict[str, object]], path: str) -> None:
    """Mark the nav section/item whose URL matches the current request path."""
    for section in sections:
        if section.get("url") == path:
            section["active"] = True
        children = section.get("children")
        if isinstance(children, list):
            for item in children:
                if isinstance(item, dict) and item.get("url") == path:
                    item["active"] = True


def _nav_context(request):
    """Build the centralized, role-aware navigation context for the shell.

    The context is intentionally prefixed with ``nav_`` so it never collides
    with per-view template context. There is no ``?nav=`` mode selector: a
    Department Administrator is always department-wide, and a Station
    Administrator is always bound to a single station.
    """
    user = request.user
    path = request.path
    if is_system_admin(user):
        sections: list[dict[str, object]] = [
            {"label": "Overview", "url": reverse("dashboard")},
            {"label": "Departments", "url": reverse("portal-system-departments")},
            {
                "label": "System Administration",
                "children": [
                    {
                        "label": "API Compatibility",
                        "url": reverse("portal-system-api-compatibility"),
                    },
                    {"label": "System Settings", "url": reverse("portal-system-settings")},
                    {"label": "Audit / System Events", "url": reverse("portal-system-audit")},
                ],
            },
        ]
        _mark_active(sections, path)
        return {
            "is_system_admin": True,
            "nav_role": "system",
            "nav_sections": sections,
            "nav_scope_label": "FireDash Server / System",
        }

    department_ids = list(active_department_ids(user))
    if department_ids:
        department = Department.objects.get(pk=department_ids[0])
        sections = [
            {"label": "Overview", "url": reverse("dashboard")},
            {
                "label": "Distributed Data",
                "children": [
                    {"label": "Data Hub", "url": reverse("portal-data-hub", args=(department.id,))},
                    {
                        "label": "Publications",
                        "url": reverse("publications-list", args=(department.id,)),
                    },
                ],
            },
            {
                "label": "Infrastructure",
                "children": [
                    {"label": "Stations", "url": reverse("portal-stations", args=(department.id,))},
                    {"label": "Tablets", "url": reverse("tablet-list", args=(department.id,))},
                ],
            },
            {
                "label": "Administration",
                "children": [
                    {
                        "label": "Administrator Accounts",
                        "url": reverse("portal-department-manage", args=(department.id,)),
                    },
                    {
                        "label": "System Settings",
                        "url": reverse("portal-department-settings", args=(department.id,)),
                    },
                    {
                        "label": "Audit Logs",
                        "url": reverse("portal-department-audit", args=(department.id,)),
                    },
                ],
            },
        ]
        _mark_active(sections, path)
        return {
            "is_system_admin": False,
            "nav_role": "department",
            "nav_sections": sections,
            "nav_department": department,
            "nav_scope_label": department.name,
            "nav_attention": attention_for_request(request, department=department),
        }

    # Station Administrator (station-only). Resolve the single authorized
    # station; an inconsistent multi-station assignment fails safely.
    station = None
    station_ambiguous = False
    try:
        station = station_admin_station(user)
    except StationAdminContextError:
        station_ambiguous = True
    if station is not None or station_ambiguous:
        sections = [{"label": "Overview", "url": reverse("dashboard")}]
        if station is not None:
            sections.append(
                {
                    "label": "Station",
                    "children": [
                        {
                            "label": "Tablets",
                            "url": reverse("tablet-list", args=(station.department_id,)),
                        },
                        {
                            "label": "Personnel",
                            "url": reverse("personnel-list", args=(station.department_id,)),
                        },
                    ],
                }
            )
        _mark_active(sections, path)
        return {
            "is_system_admin": False,
            "nav_role": "station",
            "nav_sections": sections,
            "nav_station": station,
            "nav_station_ambiguous": station_ambiguous,
            "nav_scope_label": (
                f"{station.department.name} · {station.name} · Station administration"
                if station is not None
                else "Station administrator (inconsistent configuration)"
            ),
            "nav_attention": attention_for_request(request, station=station) if station else [],
        }

    return {
        "is_system_admin": False,
        "nav_role": None,
        "nav_sections": [],
        "nav_scope_label": "",
    }


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
    context = _nav_context(request)
    if context.get("nav_role") == "department":
        context["attention"] = attention_for_request(request, department=context["nav_department"])
    elif context.get("nav_role") == "station" and context.get("nav_station") is not None:
        context["attention"] = attention_for_request(request, station=context["nav_station"])
    elif context.get("nav_role") == "system":
        context["attention"] = system_attention()
    else:
        context["attention"] = []
    return render(request, "portal/dashboard.html", context)


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
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    departments = Department.objects.all().prefetch_related(
        Prefetch(
            "memberships",
            queryset=DepartmentMembership.objects.filter(
                status=DepartmentMembership.Status.ACTIVE
            ).select_related("user"),
        )
    )
    if query:
        departments = departments.filter(Q(name__icontains=query) | Q(short_code__icontains=query))
    if status in Department.Status.values:
        departments = departments.filter(status=status)
    departments = departments.order_by("name", "short_code", "id")
    paginator = Paginator(departments, 100)
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = request.GET.copy()
    page_query.pop("page", None)
    return render(
        request,
        "portal/system_departments.html",
        {
            "departments": page.object_list,
            "form": form,
            "page": page,
            "total_count": paginator.count,
            "query": query,
            "selected_status": status,
            "page_query": page_query.urlencode(),
            "department_statuses": Department.Status.choices,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def system_api_compatibility(request: HttpRequest) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    policy = ApiVersionCompatibilityPolicy.objects.filter(api_major=1).first()
    form = ApiVersionCompatibilityPolicyForm(
        request.POST or None,
        initial={"minimum_app_version": policy.minimum_app_version if policy else None},
    )
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(
            request, return_url=reverse("portal-system-api-compatibility")
        )
        set_api_version_compatibility_policy(
            actor=request.user,
            api_major=1,
            minimum_app_version=form.cleaned_data["minimum_app_version"],
        )
        return redirect("portal-system-api-compatibility")
    policies = list(
        ApiVersionCompatibilityPolicy.objects.select_related("updated_by").order_by("api_major")
    )
    by_major = {policy.api_major: policy for policy in policies}
    v1_policy = by_major.get(1)
    rows = [
        {
            "api_major": 1,
            "policy": v1_policy,
            "minimum": v1_policy.minimum_app_version if v1_policy else None,
        }
    ]
    rows.extend(
        {"api_major": item.api_major, "policy": item, "minimum": item.minimum_app_version}
        for item in policies
        if item.api_major != 1
    )
    return render(
        request,
        "portal/system_api_compatibility.html",
        {"form": form, "policy": policy, "rows": rows},
    )


@login_required
@require_http_methods(["GET", "POST"])
def system_api_compatibility_edit_modal(request: HttpRequest, api_major: int) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    policy = ApiVersionCompatibilityPolicy.objects.filter(api_major=api_major).first()
    form = ApiVersionCompatibilityPolicyForm(
        request.POST or None,
        initial={"minimum_app_version": policy.minimum_app_version if policy else None},
    )
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(
            request, return_url=reverse("portal-system-api-compatibility")
        )
        set_api_version_compatibility_policy(
            actor=request.user,
            api_major=api_major,
            minimum_app_version=form.cleaned_data["minimum_app_version"],
        )
        if request.headers.get("HX-Request") != "true":
            return redirect("portal-system-api-compatibility")
        response = HttpResponse()
        response["HX-Redirect"] = reverse("portal-system-api-compatibility")
        return response
    return render(
        request,
        "portal/_api_compatibility_modal.html",
        {"form": form, "api_major": api_major},
    )


@login_required
@require_http_methods(["GET"])
def system_settings(request: HttpRequest) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    return render(request, "portal/system_settings.html")


@login_required
@require_http_methods(["GET"])
def system_audit(request: HttpRequest) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    query = request.GET.get("q", "").strip()
    department_id = request.GET.get("department", "")
    action = request.GET.get("action", "")
    events = AuditEvent.objects.select_related("actor_user", "department", "station")
    if department_id and department_id != "__all__":
        department = get_object_or_404(Department, pk=department_id)
        events = events.filter(department=department)
    if action:
        events = events.filter(action=action)
    if query:
        events = events.filter(
            Q(action__icontains=query)
            | Q(target_type__icontains=query)
            | Q(actor_user__display_name__icontains=query)
            | Q(actor_user__email__icontains=query)
            | Q(department__name__icontains=query)
        )
    events = events.order_by("-timestamp", "-id")
    paginator = Paginator(events, 100)
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = request.GET.copy()
    page_query.pop("page", None)
    actions = (
        AuditEvent.objects.order_by("action").values_list("action", flat=True).distinct()[:100]
    )
    return render(
        request,
        "portal/system_audit.html",
        {
            "events": page.object_list,
            "page": page,
            "total_count": paginator.count,
            "departments": Department.objects.order_by("name", "id"),
            "actions": actions,
            "query": query,
            "selected_department": department_id or "__all__",
            "selected_action": action,
            "page_query": page_query.urlencode(),
        },
    )


@login_required
@require_http_methods(["GET"])
def system_audit_detail(request: HttpRequest, event_id) -> HttpResponse:
    if not is_system_admin(request.user):
        raise PermissionDenied("System administrator role is required.")
    event = get_object_or_404(
        AuditEvent.objects.select_related("actor_user", "department", "station"), pk=event_id
    )
    return render(
        request,
        "portal/audit_event_detail.html",
        {"event": event, "back_url": reverse("portal-system-audit"), "system_scope": True},
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
            set_system_department_tablet_lease(
                actor=request.user,
                department=department,
                tablet_lease_days=lease_form.cleaned_data["tablet_lease_days"],
            )
            return redirect("portal-system-department", department_id=department.id)
    if (
        request.method == "POST"
        and request.POST.get("action") == "provision"
        and admin_form.is_valid()
    ):
        return render(
            request,
            "portal/setup_link.html",
            {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
        )
    can_bootstrap_admin = (
        department.status == Department.Status.ACTIVE
        and not DepartmentMembership.objects.filter(
            department=department,
            status=DepartmentMembership.Status.ACTIVE,
            user__is_active=True,
        ).exists()
    )
    return render(
        request,
        "portal/system_department_detail.html",
        {
            "department": department,
            "status_form": status_form,
            "admin_form": admin_form,
            "lease_form": lease_form,
            "can_bootstrap_admin": can_bootstrap_admin,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def department_manage(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    if request.method == "POST":
        require_recent_reauthentication(request, return_url=request.path)
        action = request.POST.get("action")
        if action == "grant-station":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            if DepartmentMembership.objects.filter(
                user=user,
                department=department,
            ).exists():
                raise PermissionDenied(
                    "Department Administrators are department-wide and cannot receive "
                    "station scope."
                )
            station = get_object_or_404(
                Station, pk=request.POST.get("station_id"), department=department, active=True
            )
            grant_station_admin(actor=request.user, user=user, station=station)
        elif action == "revoke-station":
            assignment = get_object_or_404(
                StationAdminAssignment,
                pk=request.POST.get("assignment_id"),
                station__department=department,
                status__in=(
                    StationAdminAssignment.Status.ACTIVE,
                    StationAdminAssignment.Status.SUSPENDED,
                ),
            )
            revoke_station_admin(actor=request.user, assignment=assignment)
        else:
            raise PermissionDenied("Unsupported administrator action.")
        return redirect("portal-department-manage", department_id=department.id)
    administrators = User.objects.filter(
        Q(department_memberships__department=department)
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
        )
    administrators = administrators.prefetch_related(
        "department_memberships", "station_admin_assignments__station"
    ).order_by("email")
    paginator = Paginator(administrators, 100)
    page = paginator.get_page(request.GET.get("page", 1))
    current_user_id = request.user.id
    return render(
        request,
        "portal/department_manage.html",
        {
            "department": department,
            "administrators": page.object_list,
            "page": page,
            "total_count": paginator.count,
            "stations": department.stations.filter(active=True).order_by("name"),
            "query": query,
            "station_filter": station_filter,
            "current_user_id": current_user_id,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def administrator_provision_modal(
    request: HttpRequest, department_id, station_id=None
) -> HttpResponse:
    department = _department_or_403(request, department_id)
    station = None
    if station_id is not None:
        station = get_object_or_404(Station, pk=station_id, department=department, active=True)
    form = AdministratorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request, return_url=request.path)
        token = (
            provision_station_admin(actor=request.user, station=station, **form.cleaned_data)
            if station is not None
            else provision_department_admin(
                actor=request.user, department=department, **form.cleaned_data
            )
        )
        return render(
            request,
            "portal/setup_link.html",
            {"setup_url": request.build_absolute_uri(reverse("accounts-setup", args=(token,)))},
        )
    return render(
        request,
        "portal/_administrator_provision_modal.html",
        {"form": form, "department": department, "station": station},
    )


@login_required
@require_http_methods(["GET", "POST"])
def administrator_detail(request: HttpRequest, department_id, user_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    administrator = get_object_or_404(User, pk=user_id)
    memberships = DepartmentMembership.objects.filter(
        user=administrator, department=department
    ).order_by("-created_at")
    assignments = (
        StationAdminAssignment.objects.filter(user=administrator, station__department=department)
        .select_related("station")
        .order_by("station__short_code")
    )
    if not memberships.exists() and not assignments.exists():
        raise PermissionDenied("Administrator is outside this department.")
    removal_form = AdministratorRemovalForm(request.POST or None)
    if request.method == "POST":
        require_recent_reauthentication(request, return_url=request.path)
        action = request.POST.get("action")
        try:
            if action == "permanent-remove" and removal_form.is_valid():
                permanently_remove_administrator(
                    actor=request.user, user=administrator, department=department
                )
                messages.success(request, "Administrator was permanently removed and anonymized.")
                return redirect("portal-department-manage", department_id=department.id)
            target = (
                get_object_or_404(
                    DepartmentMembership,
                    pk=request.POST.get("membership_id"),
                    department=department,
                    user=administrator,
                )
                if request.POST.get("membership_id")
                else get_object_or_404(
                    StationAdminAssignment,
                    pk=request.POST.get("assignment_id"),
                    station__department=department,
                    user=administrator,
                )
            )
            operations = {
                "suspend": suspend_department_admin
                if isinstance(target, DepartmentMembership)
                else suspend_station_admin,
                "reinstate": reinstate_department_admin
                if isinstance(target, DepartmentMembership)
                else reinstate_station_admin,
                "revoke": revoke_department_admin
                if isinstance(target, DepartmentMembership)
                else revoke_station_admin,
            }
            if action not in operations:
                raise ValueError("Unsupported administrator lifecycle action.")
            operations[action](actor=request.user, membership=target) if isinstance(
                target, DepartmentMembership
            ) else operations[action](actor=request.user, assignment=target)
            return redirect(request.path)
        except ValueError as error:
            messages.error(request, str(error))
    return render(
        request,
        "portal/administrator_detail.html",
        {
            "department": department,
            "administrator": administrator,
            "memberships": memberships,
            "assignments": assignments,
            "removal_form": removal_form,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def department_admin_revoke_modal(
    request: HttpRequest, department_id, membership_id
) -> HttpResponse:
    department = _department_or_403(request, department_id)
    membership = get_object_or_404(
        DepartmentMembership.objects.select_related("user"),
        pk=membership_id,
        department=department,
        status=DepartmentMembership.Status.ACTIVE,
    )
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-department-manage", args=(department.id,)),
        )
        revoke_department_admin(actor=request.user, membership=membership)
        return _modal_redirect(request, reverse("portal-department-manage", args=(department.id,)))
    return render(
        request,
        "portal/_revoke_department_admin_modal.html",
        {"department": department, "membership": membership},
    )


@login_required
@require_http_methods(["GET", "POST"])
def department_settings(request: HttpRequest, department_id) -> HttpResponse:
    """Edit only existing, authoritative per-department settings."""
    department = _department_or_403(request, department_id)
    policy = getattr(department, "personnel_retention_policy", None)
    initial = {
        "tablet_lease_days": department.tablet_lease_days,
        "retention_days": policy.retention_period.days if policy else 30,
    }
    form = DepartmentSystemSettingsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(
            request,
            return_url=reverse("portal-department-settings", args=(department.id,)),
        )
        with transaction.atomic():
            set_department_tablet_lease(
                actor=request.user,
                department=department,
                tablet_lease_days=form.cleaned_data["tablet_lease_days"],
            )
            from datetime import timedelta

            set_retention_policy(
                actor=request.user,
                department=department,
                retention_period=timedelta(days=form.cleaned_data["retention_days"]),
            )
        messages.success(request, "Department system settings were updated.")
        return redirect("portal-department-settings", department_id=department.id)
    return render(
        request,
        "portal/department_settings.html",
        {"department": department, "form": form, "retention_policy": policy},
    )


@login_required
@require_http_methods(["GET"])
def data_hub(request: HttpRequest, department_id) -> HttpResponse:
    """Gateway to bounded canonical-data modules; it deliberately owns no CRUD."""
    department = _department_or_403(request, department_id)
    # Keep this gateway read-only: the cheap authoritative counts help an
    # administrator choose a module without duplicating CRUD controls here.
    from apps.personnel.models import Person
    from apps.publications.state import scope_operational_states
    from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan

    publication_states = {
        row["dataset_type_code"]: row for row in scope_operational_states(department)
    }

    def module(*, dataset_type_code: str, **values):
        state = publication_states.get(dataset_type_code)
        # Gateway cards show only the currently distributed version. Candidate
        # work is secondary context and never becomes the displayed version
        # until the publication lifecycle activates it.
        if state is None or state["distributed_version"] is None:
            values["publication_version"] = None
            values["publication_state"] = "Not published"
        else:
            values["publication_version"] = state["distributed_version"]
            values["publication_state"] = state["current_update_label"] or state["state_label"]
        return values

    modules = (
        module(
            dataset_type_code="department_hydrants",
            name="Hydrants",
            description="Water supply reference points distributed to operational tablets.",
            count=Hydrant.objects.filter(
                department=department, status=Hydrant.Status.ACTIVE
            ).count(),
            count_label="active records",
            icon="water",
            url=reverse("reference-data-hydrants", args=(department.id,)),
        ),
        module(
            dataset_type_code="station_personnel",
            name="Personnel",
            description="Active personnel reference records and station context.",
            count=Person.objects.filter(
                department=department, lifecycle_status=Person.LifecycleStatus.ACTIVE
            ).count(),
            count_label="active records",
            icon="people",
            url=reverse("personnel-list", args=(department.id,)),
        ),
        module(
            dataset_type_code="department_fire_plans",
            name="Fire Plans",
            description="Current operational fire-plan PDFs and their canonical metadata.",
            count=FirePlan.objects.filter(department=department, active=True).count(),
            count_label="active plans",
            icon="building",
            url=reverse("reference-data-fire-plans", args=(department.id,)),
        ),
        module(
            dataset_type_code="department_klgv_plans",
            name="KLGV Plans",
            description="Optional Kleingartenverein / allotment-garden operational plans.",
            count=KlgvPlan.objects.filter(department=department, active=True).count(),
            count_label="active plans",
            icon="garden",
            url=reverse("reference-data-klgv-plans", args=(department.id,)),
        ),
    )
    return render(request, "portal/data_hub.html", {"department": department, "modules": modules})


@login_required
@require_http_methods(["GET", "POST"])
def stations(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = StationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_station(actor=request.user, department=department, **form.cleaned_data)
        return redirect("portal-stations", department_id=department.id)
    filter_form = StationListFilterForm(request.GET or None)
    queryset = department.stations.all()
    if filter_form.is_valid():
        filters = filter_form.cleaned_data
        if filters.get("q"):
            query = filters["q"]
            queryset = queryset.filter(
                Q(name__icontains=query) | Q(short_code__icontains=query) | Q(city__icontains=query)
            )
        selected_status = filters.get("active")
        if selected_status == "active":
            queryset = queryset.filter(active=True)
        elif selected_status == "inactive":
            queryset = queryset.filter(active=False)
        elif selected_status != "all":
            queryset = queryset.filter(active=True)
    else:
        queryset = queryset.filter(active=True)
    queryset = queryset.order_by("-active", "name", "short_code", "id")
    paginator = Paginator(queryset, 100)
    page = paginator.get_page(request.GET.get("page", 1))
    page_query = request.GET.copy()
    page_query.pop("page", None)
    context = {
        "department": department,
        "stations": page.object_list,
        "page": page,
        "total_count": paginator.count,
        "form": form,
        "filter_form": filter_form,
        "page_query": page_query.urlencode(),
    }
    if request.headers.get("HX-Request") == "true":
        return render(request, "portal/_station_results.html", context)
    return render(
        request,
        "portal/stations.html",
        context,
    )


def _modal(
    request: HttpRequest,
    template: str,
    context: dict[str, object] | None = None,
    **extra_context: object,
) -> HttpResponse:
    """Render a modal fragment with either a context mapping or keyword values."""
    return render(request, template, (context or {}) | extra_context)


def _modal_redirect(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
@require_http_methods(["GET", "POST"])
def station_edit_modal(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    if station.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
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
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        data["active"] = station.active
        update_station(actor=request.user, station=station, **data)
        return _modal_redirect(request, reverse("portal-station-manage", args=[station.id]))
    return _modal(
        request,
        "portal/_station_form_modal.html",
        {"form": form, "station": station, "modal_container_id": "portal-action-modal-container"},
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicle_create_modal(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    if station.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
    form = VehicleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        data["active"] = True
        create_vehicle(actor=request.user, department=station.department, station=station, **data)
        return _modal_redirect(request, reverse("portal-station-manage", args=[station.id]))
    return _modal(
        request,
        "portal/_vehicle_form_modal.html",
        {
            "form": form,
            "station": station,
            "vehicle": None,
            "modal_container_id": "portal-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicle_edit_modal(request: HttpRequest, vehicle_id) -> HttpResponse:
    vehicle = get_object_or_404(Vehicle, pk=vehicle_id)
    if vehicle.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
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
        data = form.cleaned_data
        data["active"] = vehicle.active
        update_vehicle(actor=request.user, vehicle=vehicle, **data)
        return _modal_redirect(request, reverse("portal-vehicle-manage", args=[vehicle.id]))
    return _modal(
        request,
        "portal/_vehicle_form_modal.html",
        {
            "form": form,
            "station": vehicle.station,
            "vehicle": vehicle,
            "modal_container_id": "portal-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def station_delete_modal(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    if station.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
    if request.method == "POST":
        try:
            delete_station(actor=request.user, station=station)
        except AssignmentError as error:
            return _modal(
                request,
                "portal/_delete_modal.html",
                {
                    "object": station,
                    "error": str(error),
                    "action_url": request.path,
                    "modal_container_id": "portal-action-modal-container",
                },
            )
        return _modal_redirect(request, reverse("portal-stations", args=[station.department_id]))
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": station,
            "action_url": request.path,
            "modal_container_id": "portal-action-modal-container",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicle_delete_modal(request: HttpRequest, vehicle_id) -> HttpResponse:
    vehicle = get_object_or_404(Vehicle, pk=vehicle_id)
    if vehicle.department_id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
    if request.method == "POST":
        try:
            delete_vehicle(actor=request.user, vehicle=vehicle)
        except AssignmentError as error:
            return _modal(
                request,
                "portal/_delete_modal.html",
                {
                    "object": vehicle,
                    "error": str(error),
                    "action_url": request.path,
                    "modal_container_id": "portal-action-modal-container",
                },
            )
        return _modal_redirect(request, reverse("portal-station-manage", args=[vehicle.station_id]))
    return _modal(
        request,
        "portal/_delete_modal.html",
        {
            "object": vehicle,
            "action_url": request.path,
            "modal_container_id": "portal-action-modal-container",
        },
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
    if request.method == "POST" and request.POST.get("action") == "delete":
        if request.POST.get("confirm") != "DELETE":
            messages.error(request, "Type DELETE to permanently remove this erroneous station.")
        else:
            try:
                delete_station(actor=request.user, station=station)
            except AssignmentError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "Station data permanently deleted.")
                return redirect("portal-stations", department_id=station.department_id)
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
    vehicle_status = request.GET.get("vehicle_status", "active")
    vehicles = station.vehicles.all()
    if vehicle_status == "active":
        vehicles = vehicles.filter(active=True)
    elif vehicle_status == "retired":
        vehicles = vehicles.filter(active=False)
    else:
        vehicle_status = "all"
    return render(
        request,
        "portal/station_manage.html",
        {
            "station": station,
            "department_admin": department_admin,
            "form": form,
            "admin_form": admin_form,
            "assignments": StationAdminAssignment.objects.filter(
                station=station, status=StationAdminAssignment.Status.ACTIVE
            ),
            "vehicles": vehicles.order_by("-active", "display_name", "id"),
            "vehicle_status": vehicle_status,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vehicles(request: HttpRequest, station_id) -> HttpResponse:
    station = _station_or_403(request, station_id)
    return redirect("portal-station-manage", station_id=station.id)


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
    if request.method == "POST" and request.POST.get("action") == "retire":
        try:
            deactivate_vehicle(vehicle=vehicle)
        except AssignmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Vehicle retired.")
        return redirect("portal-vehicle-manage", vehicle_id=vehicle.id)
    if request.method == "POST" and request.POST.get("action") == "delete":
        if request.POST.get("confirm") != "DELETE":
            messages.error(request, "Type DELETE to permanently remove this erroneous vehicle.")
        else:
            try:
                delete_vehicle(actor=request.user, vehicle=vehicle)
            except AssignmentError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "Vehicle data permanently deleted.")
                return redirect("portal-station-manage", station_id=vehicle.station_id)
    return render(request, "portal/vehicle_manage.html", {"vehicle": vehicle, "form": form})


@login_required
@require_http_methods(["GET"])
def department_audit(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    station_id = request.GET.get("station")
    query = request.GET.get("q", "").strip()
    events = AuditEvent.objects.filter(department=department).select_related(
        "actor_user", "station"
    )
    if station_id and station_id != "__all__":
        station = get_object_or_404(Station, pk=station_id, department=department)
        events = events.filter(station=station)
    if query:
        events = events.filter(
            Q(action__icontains=query)
            | Q(target_type__icontains=query)
            | Q(actor_user__display_name__icontains=query)
            | Q(actor_user__email__icontains=query)
        )
    events = events.order_by("-timestamp", "-id")
    paginator = Paginator(events, 100)
    page = paginator.get_page(request.GET.get("page", 1))
    stations = Station.objects.filter(department=department).order_by("name", "id")
    page_query = request.GET.copy()
    page_query.pop("page", None)
    return render(
        request,
        "portal/department_audit.html",
        {
            "department": department,
            "events": page.object_list,
            "page": page,
            "total_count": paginator.count,
            "stations": stations,
            "selected_station": station_id or "__all__",
            "query": query,
            "page_query": page_query.urlencode(),
        },
    )


@login_required
@require_http_methods(["GET"])
def department_audit_detail(request: HttpRequest, department_id, event_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    event = get_object_or_404(
        AuditEvent.objects.select_related("actor_user", "station"),
        pk=event_id,
        department=department,
    )
    return render(
        request,
        "portal/audit_event_detail.html",
        {
            "event": event,
            "department": department,
            "back_url": reverse("portal-department-audit", args=[department.id]),
        },
    )
