import base64
import json
from io import BytesIO
from urllib.parse import urlencode, urlsplit

import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from apps.accounts.reauth import require_recent_reauthentication
from apps.assignments.models import TabletVehicleAssignment
from apps.assignments.services import AssignmentError, assign_tablet_vehicle
from apps.authorization.scopes import active_department_ids
from apps.organizations.models import Department, Vehicle
from apps.tablets.forms import TabletForm, TabletReasonForm, TabletVehicleAssignmentForm
from apps.tablets.models import (
    AdoptionInvitation,
    AppInstallation,
    Tablet,
    TabletApiActivity,
)
from apps.tablets.queries import (
    current_vehicle,
    tablet_adoption_ready,
    tablet_status_counts,
    tablets_with_current_state,
)
from apps.tablets.services import (
    TabletError,
    activate_tablet,
    create_adoption_invitation,
    create_tablet,
    deactivate_tablet,
    initiate_installation_replacement,
    mark_tablet_lost,
    recover_tablet,
    retire_tablet,
    revoke_installation,
)

_TABLET_PAGE_SIZE = 100
_RECENT_API_ACTIVITY_LIMIT = 20

_SORT_FIELDS = {
    "name": "display_name",
    "status": "status",
    "asset": "asset_number",
    "created": "created_at",
    "last_seen": "last_seen",
}

# Stable physical-asset ordering for explicit state filters, with stable secondary keys.
_ACTIVE_FIRST_ORDER = Case(
    When(status=Tablet.Status.ACTIVE, then=Value(0)),
    When(status=Tablet.Status.INACTIVE, then=Value(1)),
    When(status=Tablet.Status.LOST, then=Value(2)),
    When(status=Tablet.Status.RETIRED, then=Value(3)),
    default=Value(9),
    output_field=IntegerField(),
)


def _is_hx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _validated_next(request: HttpRequest) -> str | None:
    """Return a browser-safe local continuation URL, or ``None``.

    Originating-page behaviour never trusts an arbitrary external URL: only
    same-origin local paths are honoured.
    """
    candidate = request.POST.get("next") or request.GET.get("next")
    if (
        isinstance(candidate, str)
        and candidate.startswith("/")
        and not candidate.startswith("//")
        and url_has_allowed_host_and_scheme(candidate, allowed_hosts=None)
    ):
        return candidate
    return None


def _with_next(base_url: str, next_url: str | None) -> str:
    if not next_url:
        return base_url
    return f"{base_url}?next={urlencode({'next': next_url})}"


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
    installation = request.GET.get("installation", "").strip()
    station_id = request.GET.get("station", "").strip()
    vehicle_id = request.GET.get("vehicle", "").strip()
    if query:
        queryset = queryset.filter(
            Q(display_name__icontains=query) | Q(asset_number__icontains=query)
        )
    if status:
        queryset = queryset.filter(status=status)
    else:
        # The default operational list deliberately excludes incident and historical
        # assets. LOST and RETIRED tablets remain available through the explicit
        # physical Asset State filter.
        queryset = queryset.filter(status__in=(Tablet.Status.ACTIVE, Tablet.Status.INACTIVE))
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
        installation_status=Subquery(current_installation.values("status")[:1]),
        has_open_vehicle=open_vehicle,
    )
    if installation == "current":
        queryset = queryset.filter(installation_status=AppInstallation.Status.ACTIVE)
    elif installation == "stale":
        queryset = queryset.filter(installation_status=AppInstallation.Status.STALE)
    elif installation == "none":
        queryset = queryset.filter(installation_status__isnull=True)
    key = request.GET.get("sort", "")
    descending = request.GET.get("dir") == "desc"
    if not key:
        return queryset.order_by(_ACTIVE_FIRST_ORDER, "display_name", "id")
    field = _SORT_FIELDS.get(key)
    if field is None:
        return queryset.order_by(_ACTIVE_FIRST_ORDER, "display_name", "id")
    if field == "last_seen":
        return queryset.order_by(
            F("last_seen").desc(nulls_last=True)
            if descending
            else F("last_seen").asc(nulls_last=True),
            "display_name",
            "id",
        )
    if field == "status":
        order = _ACTIVE_FIRST_ORDER.desc() if descending else _ACTIVE_FIRST_ORDER
        return queryset.order_by(order, "display_name", "id")
    direction = f"-{field}" if descending else field
    return queryset.order_by(direction, "display_name", "id")


def _page_query(request: HttpRequest) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _list_context(department: Department, request: HttpRequest) -> dict[str, object]:
    queryset = tablets_with_current_state(_tablet_queryset(department, request))
    matched_count = queryset.count()
    paginator = Paginator(queryset, _TABLET_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    station_options = [(str(s.id), s.name) for s in department.stations.order_by("name")]
    vehicle_options = [
        (str(v.id), v.display_name) for v in department.vehicles.order_by("display_name")
    ]
    return {
        "department": department,
        "tablets": page.object_list,
        "page": page,
        "page_query": _page_query(request),
        "total_count": Tablet.objects.filter(department=department).count(),
        "matched_count": matched_count,
        "statuses": Tablet.Status.choices,
        "installation_options": [
            ("current", "Current"),
            ("stale", "Stale"),
            ("none", "No installation"),
        ],
        "station_options": station_options,
        "vehicle_options": vehicle_options,
        "filters": {
            "search": request.GET.get("search", ""),
            "status": request.GET.get("status", ""),
            "installation": request.GET.get("installation", ""),
            "station": request.GET.get("station", ""),
            "vehicle": request.GET.get("vehicle", ""),
        },
        "sort": request.GET.get("sort", ""),
        "dir": request.GET.get("dir", ""),
        "list_url": request.get_full_path(),
        "results_base": reverse("tablet-list", args=(department.id,)),
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
    form = TabletForm(request.POST or None, department=department)
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
    activity = (
        TabletApiActivity.objects.filter(app_installation__tablet=tablet)
        .select_related("app_installation")
        .order_by("-occurred_at")[:_RECENT_API_ACTIVITY_LIMIT]
    )
    context = {
        "department": department,
        "tablet": tablet,
        "vehicle": vehicle,
        "installations": installations,
        "current_installation": current_installation,
        "current_installation_stale": (
            current_installation is not None
            and current_installation.status == AppInstallation.Status.STALE
        ),
        "adoption_ready": tablet_adoption_ready(tablet),
        "activity": activity,
        "activity_url": reverse("tablet-api-activity", args=(department_id, tablet.id)),
        "detail_url": reverse("tablet-detail", args=(department_id, tablet.id)),
        "assign_url": reverse("tablet-assign", args=(department_id, tablet.id)),
        "adopt_url": reverse("tablet-adopt", args=(department_id, tablet.id)),
        "replace_url": reverse("tablet-replace", args=(department_id, tablet.id)),
        "activate_url": reverse("tablet-activate", args=(department_id, tablet.id)),
        "deactivate_url": reverse("tablet-deactivate", args=(department_id, tablet.id)),
        "mark_lost_url": reverse("tablet-mark-lost", args=(department_id, tablet.id)),
        "recover_url": reverse("tablet-recover", args=(department_id, tablet.id)),
        "retire_url": reverse("tablet-retire", args=(department_id, tablet.id)),
        "revoke_installation_url": reverse(
            "tablet-revoke-installation", args=(department_id, tablet.id)
        ),
    }
    return render(request, "tablets/detail.html", context)


@require_http_methods(["GET"])
@login_required
def tablet_api_activity(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    activity = (
        TabletApiActivity.objects.filter(app_installation__tablet=tablet)
        .select_related("app_installation")
        .order_by("-occurred_at")[:_RECENT_API_ACTIVITY_LIMIT]
    )
    return render(
        request,
        "tablets/_tablet_api_activity.html",
        {
            "department": tablet.department,
            "tablet": tablet,
            "activity": activity,
            "activity_url": reverse("tablet-api-activity", args=(department_id, tablet.id)),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_assign(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicles = _eligible_vehicles(tablet)
    vehicle = current_vehicle(tablet)
    next_url = _validated_next(request)
    return_url = next_url or reverse("tablet-detail", args=(department_id, tablet.id))
    form = TabletVehicleAssignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        selected = get_object_or_404(Vehicle, pk=form.cleaned_data["vehicle_id"])
        try:
            assign_tablet_vehicle(tablet=tablet, vehicle=selected, actor=request.user)
        except AssignmentError as error:
            form.add_error(None, str(error))
        else:
            verb = "transferred" if vehicle is not None else "assigned"
            messages.success(
                request,
                f'Tablet "{tablet.display_name}" was {verb} to {selected.display_name}.',
            )
            if _is_hx(request):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = return_url
                return response
            return redirect(return_url)
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
                "next": return_url,
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
            "next": return_url,
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
    next_url = _validated_next(request)
    return_url = next_url or reverse("tablet-detail", args=(department_id, tablet.id))
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=_with_next(
                reverse("tablet-adopt", args=(department_id, tablet.id)), next_url
            ),
        )
        try:
            invitation, token = create_adoption_invitation(actor=request.user, tablet=tablet)
        except TabletError as error:
            messages.error(request, f"Could not start tablet adoption. {_adoption_issue(error)}")
            return redirect(return_url)
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
                "next": return_url,
            },
        )
    return render(
        request,
        "tablets/adoption_confirm.html",
        {
            "tablet": tablet,
            "department": tablet.department,
            "vehicle": vehicle,
            "mode": "adoption",
            "next": return_url,
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_replace(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    next_url = _validated_next(request)
    return_url = next_url or reverse("tablet-detail", args=(department_id, tablet.id))
    if request.method == "POST":
        require_recent_reauthentication(
            request,
            return_url=_with_next(
                reverse("tablet-replace", args=(department_id, tablet.id)), next_url
            ),
        )
        try:
            invitation, token = initiate_installation_replacement(actor=request.user, tablet=tablet)
        except TabletError as error:
            messages.error(request, f"Could not start re-provisioning. {_adoption_issue(error)}")
            return redirect(return_url)
        return _render_invitation(
            request,
            mode="replacement",
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
                "mode": "replacement",
                "next": return_url,
            },
        )
    return render(
        request,
        "tablets/adoption_confirm.html",
        {
            "tablet": tablet,
            "department": tablet.department,
            "vehicle": vehicle,
            "mode": "replacement",
            "next": return_url,
        },
    )


def _qr_data_uri(token: str) -> str:
    origin = settings.FIREDASH_PUBLIC_ORIGIN
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise TabletError("FireDash public origin must be an HTTPS origin.")
    payload = json.dumps(
        {"origin": origin.rstrip("/"), "protocol": "firedash-provisioning-v1", "token": token},
        separators=(",", ":"),
        sort_keys=True,
    )
    image = qrcode.make(payload).get_image()
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _render_invitation(request, *, mode, token, invitation, tablet, vehicle) -> HttpResponse:
    context = {
        "mode": mode,
        "token": token,
        "origin": settings.FIREDASH_PUBLIC_ORIGIN.rstrip("/"),
        "invitation": invitation,
        "tablet": tablet,
        "vehicle": vehicle,
        "department": tablet.department,
        "qr_code": _qr_data_uri(token),
        "expires_at": invitation.expires_at,
        "state": "waiting",
        "status_url": reverse(
            "tablet-adoption-status", args=(tablet.department_id, tablet.id, invitation.id)
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
                "tablet-adoption-status", args=(tablet.department_id, tablet.id, invitation.id)
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


def _asset_action(
    request: HttpRequest,
    department_id,
    tablet_id,
    *,
    service,
    title: str,
    description: str,
    submit_label: str,
    danger: bool = False,
    needs_reason: bool = False,
    reauth: bool = False,
    verb: str,
) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    next_url = _validated_next(request)
    return_url = next_url or reverse("tablet-detail", args=(department_id, tablet.id))
    form = TabletReasonForm(request.POST or None) if needs_reason else None
    if request.method == "POST" and (form is None or form.is_valid()):
        if reauth:
            require_recent_reauthentication(request, return_url=_with_next(request.path, next_url))
        try:
            if needs_reason:
                assert form is not None
                service(actor=request.user, tablet=tablet, reason=form.cleaned_data["reason"])
            else:
                service(actor=request.user, tablet=tablet)
        except TabletError as error:
            if form is not None:
                form.add_error(None, str(error))
            else:
                messages.error(request, str(error))
                return redirect(return_url)
        else:
            messages.success(request, f'Tablet "{tablet.display_name}" was {verb}.')
            return redirect(return_url)
    context = {
        "title": title,
        "description": description,
        "submit_label": submit_label,
        "danger": danger,
        "action_url": request.path,
        "tablet": tablet,
        "department": tablet.department,
        "vehicle": vehicle,
        "form": form,
        "next": return_url,
    }
    if _is_hx(request):
        return render(request, "tablets/_lifecycle_confirm_modal.html", context)
    return render(request, "tablets/lifecycle_confirm.html", context)


@require_http_methods(["GET", "POST"])
@login_required
def tablet_activate(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    return _asset_action(
        request,
        department_id,
        tablet_id,
        service=activate_tablet,
        title="Activate tablet",
        description="Commission this tablet into operational service.",
        submit_label="Activate",
        verb="activated",
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_deactivate(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    return _asset_action(
        request,
        department_id,
        tablet_id,
        service=deactivate_tablet,
        title="Deactivate tablet",
        description=(
            "Remove this tablet from operational service. Its current installation "
            "remains known, but cannot access operational data until the tablet is activated again."
        ),
        submit_label="Deactivate",
        danger=True,
        needs_reason=True,
        reauth=True,
        verb="deactivated",
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_mark_lost(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    return _asset_action(
        request,
        department_id,
        tablet_id,
        service=mark_tablet_lost,
        title="Mark tablet lost",
        description=(
            "Record that this hardware cannot currently be accounted for. Its current "
            "installation will be revoked and its data access withdrawn."
        ),
        submit_label="Mark lost",
        danger=True,
        needs_reason=True,
        reauth=True,
        verb="marked lost",
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_recover(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    return _asset_action(
        request,
        department_id,
        tablet_id,
        service=recover_tablet,
        title="Mark tablet recovered",
        description=(
            "Return this lost tablet to the inactive stock pool for inspection and "
            "re-provisioning. It is not recommissioned automatically."
        ),
        submit_label="Mark recovered",
        verb="marked recovered",
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_retire(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    return _asset_action(
        request,
        department_id,
        tablet_id,
        service=retire_tablet,
        title="Retire tablet",
        description=(
            "Permanently withdraw this tablet from operational service. Its current "
            "installation will be revoked and its data access withdrawn."
        ),
        submit_label="Retire",
        danger=True,
        needs_reason=True,
        reauth=True,
        verb="retired",
    )


@require_http_methods(["GET", "POST"])
@login_required
def tablet_revoke_installation(request: HttpRequest, department_id, tablet_id) -> HttpResponse:
    tablet = _tablet_or_404(request, department_id, tablet_id)
    vehicle = current_vehicle(tablet)
    next_url = _validated_next(request)
    return_url = next_url or reverse("tablet-detail", args=(department_id, tablet.id))
    form = TabletReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        require_recent_reauthentication(request, return_url=_with_next(request.path, next_url))
        installation = (
            tablet.installations.filter(
                status__in=(AppInstallation.Status.ACTIVE, AppInstallation.Status.STALE)
            )
            .order_by("-adopted_at")
            .first()
        )
        if installation is None:
            messages.error(request, "No current installation to revoke.")
            return redirect(return_url)
        try:
            revoke_installation(
                actor=request.user,
                installation=installation,
                reason=form.cleaned_data["reason"],
            )
        except TabletError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f'The installation for "{tablet.display_name}" was revoked.')
            return redirect(return_url)
    context = {
        "title": "Revoke installation",
        "description": (
            "Revoke this tablet's current installation authorization. The installation "
            "keeps its purge directive and can no longer access data."
        ),
        "submit_label": "Revoke installation",
        "danger": True,
        "action_url": request.path,
        "tablet": tablet,
        "department": tablet.department,
        "vehicle": vehicle,
        "form": form,
        "next": return_url,
    }
    if _is_hx(request):
        return render(request, "tablets/_lifecycle_confirm_modal.html", context)
    return render(request, "tablets/lifecycle_confirm.html", context)
