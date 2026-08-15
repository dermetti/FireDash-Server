import base64
from io import BytesIO

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.assignments.models import TabletVehicleAssignment
from apps.assignments.services import AssignmentError, assign_tablet_vehicle
from apps.authorization.scopes import active_department_ids
from apps.organizations.models import Department, Vehicle
from apps.tablets.forms import TabletForm, TabletRemovalForm, TabletVehicleAssignmentForm
from apps.tablets.models import AdoptionInvitation, AppInstallation, ReactivationInvitation, Tablet
from apps.tablets.queries import (
    current_vehicle,
    tablet_adoption_ready,
    tablet_status_counts,
    tablets_with_current_state,
)
from apps.tablets.services import (
    TabletError,
    create_adoption_invitation,
    create_reactivation_invitation,
    create_tablet,
    remove_tablet,
)

_SORT_FIELDS = {
    "name": "display_name",
    "status": "status",
    "asset": "asset_number",
    "created": "created_at",
    "last_seen": "last_seen",
}


def _is_hx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _department_or_403(request: HttpRequest, department_id) -> Department:
    department = get_object_or_404(Department, pk=department_id)
    if department.id not in active_department_ids(request.user):
        raise PermissionDenied("Department administrator role is required.")
    return department


def _tablet_or_404(request: HttpRequest, department_id, tablet_id) -> Tablet:
    department = _department_or_403(request, department_id)
    return get_object_or_404(Tablet, pk=tablet_id, department=department)


def _eligible_vehicles(tablet: Tablet):
    return (
        Vehicle.objects.filter(department=tablet.department, active=True, station__active=True)
        .select_related("station")
        .order_by("display_name")
    )


def _tablet_queryset(department: Department, request: HttpRequest):
    queryset = Tablet.objects.filter(department=department)
    query = request.GET.get("search", "").strip()
    status = request.GET.get("status", "").strip()
    station_id = request.GET.get("station", "").strip()
    vehicle_id = request.GET.get("vehicle", "").strip()
    if query:
        queryset = queryset.filter(
            Q(display_name__icontains=query) | Q(asset_number__icontains=query)
        )
    if status:
        queryset = queryset.filter(status=status)
    if station_id:
        queryset = queryset.filter(
            vehicle_assignments__valid_until__isnull=True,
            vehicle_assignments__ended_at__isnull=True,
            vehicle_assignments__vehicle__station_id=station_id,
        )
    if vehicle_id:
        queryset = queryset.filter(
            vehicle_assignments__valid_until__isnull=True,
            vehicle_assignments__ended_at__isnull=True,
            vehicle_assignments__vehicle_id=vehicle_id,
        )
    current_installation = AppInstallation.objects.filter(
        tablet=OuterRef("pk"),
        status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE),
    ).order_by("-adopted_at")
    open_vehicle = Exists(
        TabletVehicleAssignment.objects.filter(
            tablet=OuterRef("pk"),
            valid_until__isnull=True,
            ended_at__isnull=True,
            vehicle__active=True,
            vehicle__station__active=True,
        )
    )
    queryset = queryset.annotate(
        last_seen=Subquery(current_installation.values("last_successful_check_in_at")[:1]),
        has_open_vehicle=open_vehicle,
    )
    key = request.GET.get("sort", "name")
    field = _SORT_FIELDS.get(key, "display_name")
    descending = request.GET.get("dir") == "desc"
    if field == "last_seen":
        return queryset.order_by(
            F("last_seen").desc(nulls_last=True)
            if descending
            else F("last_seen").asc(nulls_last=True)
        )
    return queryset.order_by(f"-{field}" if descending else field)


def _list_context(department: Department, request: HttpRequest) -> dict[str, object]:
    queryset = tablets_with_current_state(_tablet_queryset(department, request))
    total = Tablet.objects.filter(department=department).count()
    station_options = [(str(s.id), s.name) for s in department.stations.order_by("name")]
    vehicle_options = [
        (str(v.id), v.display_name) for v in department.vehicles.order_by("display_name")
    ]
    return {
        "department": department,
        "tablets": queryset,
        "total_count": total,
        "statuses": Tablet.Status.choices,
        "station_options": station_options,
        "vehicle_options": vehicle_options,
        "filters": {
            "search": request.GET.get("search", ""),
            "status": request.GET.get("status", ""),
            "station": request.GET.get("station", ""),
            "vehicle": request.GET.get("vehicle", ""),
        },
        "sort": request.GET.get("sort", "name"),
        "dir": request.GET.get("dir", ""),
    }


@require_http_methods(["GET"])
@login_required
def tablet_list(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    # The canonical URL serves both full pages and, for HTMX filter/sort requests,
    # only the results partial. This keeps `hx-push-url` on the canonical URL so a
    # direct reload renders a complete page rather than a bare partial.
    if _is_hx(request):
        return render(request, "tablets/_tablet_results.html", _list_context(department, request))
    context = _list_context(department, request)
    context["counts"] = tablet_status_counts(department)
    context["last_updated"] = timezone.now()
    return render(request, "tablets/list.html", context)


@require_http_methods(["GET"])
@login_required
def tablet_status_summary(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    return render(
        request,
        "tablets/_tablet_status_summary.html",
        {
            "department": department,
            "counts": tablet_status_counts(department),
            "last_updated": timezone.now(),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_create(request: HttpRequest, department_id) -> HttpResponse:
    department = _department_or_403(request, department_id)
    form = TabletForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        tablet = create_tablet(actor=request.user, department=department, **form.cleaned_data)
        messages.success(request, f'Tablet "{tablet.display_name}" was registered successfully.')
        if _is_hx(request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("tablet-list", args=(department.id,))
            return response
        return redirect("tablet-list", department_id=department.id)
    if _is_hx(request):
        return render(
            request, "tablets/_create_modal.html", {"form": form, "department": department}
        )
    return render(request, "tablets/create.html", {"form": form, "department": department})


@require_http_methods(["GET"])
@login_required
def tablet_detail(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    department = tablet.department
    vehicle = current_vehicle(tablet)
    installations = tablet.installations.order_by("-adopted_at")
    current_installation = installations.filter(
        status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
    ).first()
    context = {
        "department": department,
        "tablet": tablet,
        "vehicle": vehicle,
        "installations": installations,
        "current_installation": current_installation,
        "adoption_ready": tablet_adoption_ready(tablet),
    }
    return render(request, "tablets/detail.html", context)


@require_http_methods(["GET", "POST"])
@login_required
def tablet_assign(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicles = _eligible_vehicles(tablet)
    vehicle = current_vehicle(tablet)
    form = TabletVehicleAssignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        selected = get_object_or_404(Vehicle, pk=form.cleaned_data["vehicle_id"])
        try:
            assign_tablet_vehicle(tablet=tablet, vehicle=selected, actor=request.user)
        except AssignmentError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request, f'Tablet "{tablet.display_name}" was assigned to {selected.display_name}.'
            )
            if _is_hx(request):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = reverse("tablet-detail", args=(department_id, tablet.id))
                return response
            return redirect("tablet-detail", department_id=department_id, tablet_id=tablet.id)
    if _is_hx(request):
        return render(
            request,
            "tablets/_assign_modal.html",
            {
                "form": form,
                "tablet": tablet,
                "department": tablet.department,
                "vehicles": vehicles,
                "vehicle": vehicle,
            },
        )
    return render(
        request,
        "tablets/assign.html",
        {
            "form": form,
            "tablet": tablet,
            "department": tablet.department,
            "vehicles": vehicles,
            "vehicle": vehicle,
        },
    )


def _adoption_issue(error: TabletError) -> str:
    message = str(error)
    if "vehicle assignment" in message:
        return "The tablet is not assigned to an operational vehicle."
    if "department must be active" in message:
        return "The tablet's department is not active."
    return "The tablet cannot be adopted in its current state."


@require_http_methods(["GET", "POST"])
@login_required
def tablet_adopt(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=reverse("tablet-adopt", args=(department_id, tablet.id)),
        )
        try:
            invitation, token = create_adoption_invitation(actor=request.user, tablet=tablet)
        except TabletError as error:
            messages.error(request, f"Could not start tablet adoption. {_adoption_issue(error)}")
            return redirect("tablet-detail", department_id=department_id, tablet_id=tablet.id)
        return _render_invitation(
            request,
            mode="adoption",
            token=token,
            invitation=invitation,
            tablet=tablet,
            vehicle=vehicle,
        )
    if _is_hx(request):
        return render(
            request,
            "tablets/_adoption_confirm_modal.html",
            {
                "tablet": tablet,
                "department": tablet.department,
                "vehicle": vehicle,
                "mode": "adoption",
            },
        )
    return render(
        request,
        "tablets/adoption_confirm.html",
        {"tablet": tablet, "department": tablet.department, "vehicle": vehicle, "mode": "adoption"},
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_reactivate(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    installation = (
        tablet.installations.filter(status=AppInstallation.Status.STALE)
        .order_by("-adopted_at")
        .first()
    )
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=reverse("tablet-reactivate", args=(department_id, tablet.id)),
        )
        if installation is None:
            messages.error(request, "Only a stale tablet can be reactivated.")
            return redirect("tablet-detail", department_id=department_id, tablet_id=tablet.id)
        try:
            invitation, token = create_reactivation_invitation(
                actor=request.user, installation=installation
            )
        except TabletError as error:
            messages.error(request, f"Could not start reactivation. {_adoption_issue(error)}")
            return redirect("tablet-detail", department_id=department_id, tablet_id=tablet.id)
        return _render_invitation(
            request,
            mode="reactivation",
            token=token,
            invitation=invitation,
            tablet=tablet,
            vehicle=vehicle,
        )
    if _is_hx(request):
        return render(
            request,
            "tablets/_adoption_confirm_modal.html",
            {
                "tablet": tablet,
                "department": tablet.department,
                "vehicle": vehicle,
                "mode": "reactivation",
            },
        )
    return render(
        request,
        "tablets/adoption_confirm.html",
        {
            "tablet": tablet,
            "department": tablet.department,
            "vehicle": vehicle,
            "mode": "reactivation",
        },
    )


def _qr_data_uri(token: str) -> str:
    image = qrcode.make(token).get_image()
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _render_invitation(request, *, mode, token, invitation, tablet, vehicle) -> HttpResponse:
    context = {
        "mode": mode,
        "token": token,
        "invitation": invitation,
        "tablet": tablet,
        "vehicle": vehicle,
        "department": tablet.department,
        "qr_code": _qr_data_uri(token),
        "expires_at": invitation.expires_at,
        "state": "waiting",
        "status_url": reverse(
            "tablet-reactivation-status" if mode == "reactivation" else "tablet-adoption-status",
            args=(tablet.department_id, tablet.id, invitation.id),
        ),
    }
    return render(request, "tablets/invitation.html", context)


def _invitation_state(invitation) -> str:
    if invitation.used_at:
        return "completed"
    if invitation.revoked_at:
        return "revoked"
    if invitation.expires_at <= timezone.now():
        return "expired"
    return "waiting"


def _adoption_status_response(request, department_id, tablet_id, invitation, mode) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    state = _invitation_state(invitation)
    return render(
        request,
        "tablets/_adoption_status.html",
        {
            "state": state,
            "mode": mode,
            "tablet": tablet,
            "department": tablet.department,
            "invitation": invitation,
            "status_url": reverse(
                "tablet-reactivation-status"
                if mode == "reactivation"
                else "tablet-adoption-status",
                args=(tablet.department_id, tablet.id, invitation.id),
            ),
        },
    )


@require_http_methods(["GET"])
@login_required
def tablet_adoption_status(
    request: HttpRequest, department_id, tablet_id, invitation_id
) -> HttpResponse:
    invitation = get_object_or_404(AdoptionInvitation, pk=invitation_id, tablet_id=tablet_id)
    return _adoption_status_response(request, department_id, tablet_id, invitation, mode="adoption")


@require_http_methods(["GET"])
@login_required
def tablet_reactivation_status(
    request: HttpRequest, department_id, tablet_id, invitation_id
) -> HttpResponse:
    invitation = get_object_or_404(
        ReactivationInvitation, pk=invitation_id, app_installation__tablet_id=tablet_id
    )
    return _adoption_status_response(
        request, department_id, tablet_id, invitation, mode="reactivation"
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_remove(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    form = TabletRemovalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(
            request,
            return_url=reverse("tablet-remove", args=(department_id, tablet.id)),
        )
        try:
            remove_tablet(
                actor=request.user,
                tablet=tablet,
                status=form.cleaned_data["status"],
                reason=form.cleaned_data["reason"],
            )
        except TabletError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f'Tablet "{tablet.display_name}" was removed.')
            return redirect("tablet-detail", department_id=department_id, tablet_id=tablet.id)
    if _is_hx(request):
        return render(
            request,
            "tablets/_remove_modal.html",
            {"form": form, "tablet": tablet, "department": tablet.department, "vehicle": vehicle},
        )
    return render(
        request,
        "tablets/remove.html",
        {"form": form, "tablet": tablet, "department": tablet.department, "vehicle": vehicle},
    )
