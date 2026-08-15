"""Department-admin tablet management UI and adoption workflow tests (PostgreSQL-backed)."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet
from apps.tablets.queries import tablet_adoption_ready, tablet_status_counts
from apps.tablets.services import create_adoption_invitation


@pytest.fixture
def tablet_ui_scope(db):
    admin = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    other_admin = User.objects.create_user("other@example.test", "Other", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other_department = Department.objects.create(name="Bravo", short_code="BRV", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(
        user=other_admin, department=other_department, created_by=admin
    )
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    other_station = Station.objects.create(
        department=department, name="Station B", short_code="STB"
    )
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Engine 1"
    )
    other_vehicle = Vehicle.objects.create(
        department=other_department,
        station=Station.objects.create(
            department=other_department, name="Bravo Station", short_code="BST"
        ),
        display_name="Bravo Engine",
    )
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    return SimpleNamespace(
        admin=admin,
        station_admin=station_admin,
        other_admin=other_admin,
        department=department,
        other_department=other_department,
        station=station,
        other_station=other_station,
        vehicle=vehicle,
        other_vehicle=other_vehicle,
    )


def _login_with_reauth(client, user):
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


def _pending_tablet(department, **kwargs):
    return Tablet.objects.create(department=department, display_name="Command iPad", **kwargs)


def _assign(tablet, vehicle, actor):
    return TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=actor
    )


# --- query / status ---------------------------------------------------------


@pytest.mark.django_db
def test_tablet_status_counts_are_department_scoped(tablet_ui_scope):
    scope = tablet_ui_scope
    _pending_tablet(scope.department, status=Tablet.Status.PENDING)
    _pending_tablet(scope.department, status=Tablet.Status.ACTIVE)
    _pending_tablet(scope.department, status=Tablet.Status.STALE)
    _pending_tablet(scope.department, status=Tablet.Status.RETIRED)
    _pending_tablet(scope.other_department, status=Tablet.Status.ACTIVE)

    counts = tablet_status_counts(scope.department)
    assert counts == {
        "total": 4,
        "active": 1,
        "pending": 1,
        "stale": 1,
        "removed": 0,
        "lost": 0,
        "retired": 1,
    }


@pytest.mark.django_db
def test_tablet_adoption_ready_requires_open_vehicle(tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    assert tablet_adoption_ready(tablet) is False
    _assign(tablet, scope.vehicle, scope.admin)
    assert tablet_adoption_ready(tablet) is True


# --- permissions ------------------------------------------------------------


@pytest.mark.django_db
def test_department_admin_can_view_tablet_pages(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.admin)

    assert client.get(reverse("tablet-list", args=(scope.department.id,))).status_code == 200
    assert (
        client.get(reverse("tablet-detail", args=(scope.department.id, tablet.id))).status_code
        == 200
    )
    assert (
        client.get(reverse("tablet-status-summary", args=(scope.department.id,))).status_code == 200
    )


@pytest.mark.django_db
def test_station_admin_is_denied_tablet_pages(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.station_admin)

    assert client.get(reverse("tablet-list", args=(scope.department.id,))).status_code == 403
    assert (
        client.get(reverse("tablet-detail", args=(scope.department.id, tablet.id))).status_code
        == 403
    )


@pytest.mark.django_db
def test_other_department_admin_is_denied(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.other_department)
    client.force_login(scope.admin)

    assert client.get(reverse("tablet-list", args=(scope.other_department.id,))).status_code == 403
    assert (
        client.get(
            reverse("tablet-detail", args=(scope.other_department.id, tablet.id))
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_unauthenticated_is_redirected(client, tablet_ui_scope):
    scope = tablet_ui_scope
    url = reverse("tablet-list", args=(scope.department.id,))
    response = client.get(url)
    assert response.status_code == 302
    assert "/accounts/" in response.url


# --- create tablet ----------------------------------------------------------


@pytest.mark.django_db
def test_create_tablet_via_service_requires_department_admin(client, tablet_ui_scope):
    scope = tablet_ui_scope
    _login_with_reauth(client, scope.admin)
    response = client.post(
        reverse("tablet-create", args=(scope.department.id,)),
        {"display_name": "Command iPad", "asset_number": "TAB-1"},
    )
    assert response.status_code == 302
    assert Tablet.objects.filter(display_name="Command iPad", department=scope.department).exists()


@pytest.mark.django_db
def test_create_tablet_requires_display_name(client, tablet_ui_scope):
    scope = tablet_ui_scope
    client.force_login(scope.admin)
    response = client.post(
        reverse("tablet-create", args=(scope.department.id,)),
        {"display_name": "  ", "asset_number": ""},
    )
    assert response.status_code == 200
    assert Tablet.objects.count() == 0


@pytest.mark.django_db
def test_create_tablet_hx_modal_load(client, tablet_ui_scope):
    scope = tablet_ui_scope
    client.force_login(scope.admin)
    response = client.get(
        reverse("tablet-create", args=(scope.department.id,)), HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert "Register tablet" in response.content.decode()


# --- vehicle assignment -----------------------------------------------------


@pytest.mark.django_db
def test_assign_vehicle_same_department(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.admin)
    response = client.post(
        reverse("tablet-assign", args=(scope.department.id, tablet.id)),
        {"vehicle_id": str(scope.vehicle.id)},
    )
    assert response.status_code == 302
    assert tablet.vehicle_assignments.filter(vehicle=scope.vehicle, ended_at__isnull=True).exists()


@pytest.mark.django_db
def test_assign_vehicle_cross_department_rejected(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.admin)
    response = client.post(
        reverse("tablet-assign", args=(scope.department.id, tablet.id)),
        {"vehicle_id": str(scope.other_vehicle.id)},
    )
    assert response.status_code == 200
    assert not tablet.vehicle_assignments.exists()


@pytest.mark.django_db
def test_assign_vehicle_audits_event(client, tablet_ui_scope):
    from apps.audit.models import AuditEvent

    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.admin)
    client.post(
        reverse("tablet-assign", args=(scope.department.id, tablet.id)),
        {"vehicle_id": str(scope.vehicle.id)},
    )
    assert AuditEvent.objects.filter(
        action="tablet.vehicle_assigned", target_type="tablet_vehicle_assignment"
    ).exists()


# --- list / filter ----------------------------------------------------------


@pytest.mark.django_db
def test_tablet_list_filters(client, tablet_ui_scope):
    scope = tablet_ui_scope
    active = _pending_tablet(scope.department, status=Tablet.Status.ACTIVE, asset_number="CMD-01")
    _pending_tablet(scope.department, status=Tablet.Status.STALE, display_name="Reserve")
    client.force_login(scope.admin)

    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"search": "CMD", "status": "ACTIVE"}
    )
    assert list(response.context["tablets"]) == [active]


@pytest.mark.django_db
def test_tablet_list_htx_returns_partial_and_direct_reload_renders_full_page(
    client, tablet_ui_scope
):
    scope = tablet_ui_scope
    _pending_tablet(scope.department)
    client.force_login(scope.admin)

    # HTMX filter/sort requests hit the canonical URL and receive only the results partial.
    partial = client.get(
        reverse("tablet-list", args=(scope.department.id,)),
        {"status": "PENDING"},
        HTTP_HX_REQUEST="true",
    )
    assert partial.status_code == 200
    partial_html = partial.content.decode()
    assert "tablet-results" in partial_html
    assert "<html" not in partial_html

    # The same URL without the HX header must render the complete management page so a
    # direct reload of a pushed history URL never lands on a bare partial.
    full = client.get(reverse("tablet-list", args=(scope.department.id,)), {"status": "PENDING"})
    assert full.status_code == 200
    full_html = full.content.decode()
    assert "<html" in full_html
    assert "tablet-results" in full_html
    assert "Devices / Tablets" in full_html


@pytest.mark.django_db
def test_tablet_list_partial_requires_department_admin(client, tablet_ui_scope):
    scope = tablet_ui_scope
    client.force_login(scope.station_admin)
    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 403


# --- detail / secret exposure ----------------------------------------------


def _adopted_installation(tablet, credential_hash="f" * 64, fingerprint="b" * 64):
    now = timezone.now()
    return AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash=credential_hash,
        app_version="9.9.9",
        hpke_public_key=b"public",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint=fingerprint,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=7),
    )


@pytest.mark.django_db
def test_ordinary_pages_never_expose_device_secrets(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department, status=Tablet.Status.ACTIVE)
    _adopted_installation(tablet)
    _, token = create_adoption_invitation(actor=scope.admin, tablet=tablet)
    client.force_login(scope.admin)

    list_html = client.get(reverse("tablet-list", args=(scope.department.id,))).content.decode()
    detail_html = client.get(
        reverse("tablet-detail", args=(scope.department.id, tablet.id))
    ).content.decode()
    status_html = client.get(
        reverse("tablet-status-summary", args=(scope.department.id,))
    ).content.decode()

    for secret in ("f" * 64, token, "publication-kek", "credential"):
        assert secret not in list_html
        assert secret not in detail_html
        assert secret not in status_html


# --- adoption ---------------------------------------------------------------


@pytest.mark.django_db
def test_adoption_get_renders_confirmation_without_token(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    _assign(tablet, scope.vehicle, scope.admin)
    client.force_login(scope.admin)

    response = client.get(reverse("tablet-adopt", args=(scope.department.id, tablet.id)))
    html = response.content.decode()
    assert response.status_code == 200
    assert "Start tablet adoption" in html
    assert AdoptionInvitation.objects.count() == 0


@pytest.mark.django_db
def test_adoption_post_requires_reauth(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    _assign(tablet, scope.vehicle, scope.admin)
    client.force_login(scope.admin)

    response = client.post(reverse("tablet-adopt", args=(scope.department.id, tablet.id)))
    assert response.status_code == 302
    assert "reauthenticate" in response.url
    assert client.session["pending_reauth"]["return_url"] == reverse(
        "tablet-adopt", args=(scope.department.id, tablet.id)
    )
    assert AdoptionInvitation.objects.count() == 0


@pytest.mark.django_db
def test_adoption_post_creates_invitation_and_shows_token_once(
    client, tablet_ui_scope, monkeypatch
):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    _assign(tablet, scope.vehicle, scope.admin)
    _login_with_reauth(client, scope.admin)

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        "apps.tablets.views._qr_data_uri", lambda token: captured.update(token=token) or "FAKEQR"
    )

    response = client.post(reverse("tablet-adopt", args=(scope.department.id, tablet.id)))
    assert response.status_code == 200
    invitation = AdoptionInvitation.objects.get(tablet=tablet)
    assert invitation.used_at is None
    html = response.content.decode()

    # QR rendered and payload equals the raw text token.
    assert "data:image/png;base64,FAKEQR" in html
    assert captured["token"] in html

    # Raw token is not in the invitation DB record (only its hash is).
    assert captured["token"] not in invitation.token_hash

    # The raw token appears only on the immediate show-once response.
    token = captured["token"]
    assert (
        token
        not in client.get(reverse("tablet-list", args=(scope.department.id,))).content.decode()
    )
    assert (
        token
        not in client.get(
            reverse("tablet-detail", args=(scope.department.id, tablet.id))
        ).content.decode()
    )
    assert (
        token
        not in client.get(
            reverse("tablet-status-summary", args=(scope.department.id,))
        ).content.decode()
    )
    assert token not in str(client.session)
    assert (
        token
        not in client.get(
            reverse("tablet-adopt", args=(scope.department.id, tablet.id))
        ).content.decode()
    )


@pytest.mark.django_db
def test_adoption_requires_vehicle_assignment(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)  # no vehicle
    _login_with_reauth(client, scope.admin)

    response = client.post(reverse("tablet-adopt", args=(scope.department.id, tablet.id)))
    assert response.status_code == 302  # redirect back to detail with error message
    assert AdoptionInvitation.objects.count() == 0


@pytest.mark.django_db
def test_adoption_status_polling_states(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    _assign(tablet, scope.vehicle, scope.admin)
    _login_with_reauth(client, scope.admin)
    client.post(reverse("tablet-adopt", args=(scope.department.id, tablet.id)))
    invitation = AdoptionInvitation.objects.get(tablet=tablet)

    url = reverse("tablet-adoption-status", args=(scope.department.id, tablet.id, invitation.id))
    waiting = client.get(url)
    assert "Waiting for tablet" in waiting.content.decode()

    invitation.expires_at = timezone.now() - timedelta(seconds=1)
    invitation.save(update_fields=("expires_at",))
    expired = client.get(url)
    assert "expired" in expired.content.decode()

    invitation.expires_at = timezone.now() + timedelta(minutes=15)
    invitation.used_at = timezone.now()
    invitation.save(update_fields=("expires_at", "used_at"))
    completed = client.get(url)
    assert "adopted successfully" in completed.content.decode()


# --- reactivation -----------------------------------------------------------


@pytest.mark.django_db
def test_reactivation_for_stale_tablet(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department, status=Tablet.Status.STALE)
    _assign(tablet, scope.vehicle, scope.admin)
    installation = _adopted_installation(tablet)
    installation.status = AppInstallation.Status.STALE
    installation.stale_at = timezone.now()
    installation.save(update_fields=("status", "stale_at"))
    _login_with_reauth(client, scope.admin)

    response = client.post(reverse("tablet-reactivate", args=(scope.department.id, tablet.id)))
    assert response.status_code == 200
    assert "reactivation" in response.content.decode()


# --- removal ----------------------------------------------------------------


@pytest.mark.django_db
def test_remove_tablet_marks_lost(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    _login_with_reauth(client, scope.admin)

    response = client.post(
        reverse("tablet-remove", args=(scope.department.id, tablet.id)),
        {"status": Tablet.Status.LOST, "reason": "Lost on scene"},
    )
    assert response.status_code == 302
    tablet.refresh_from_db()
    assert tablet.status == Tablet.Status.LOST
    assert tablet.active is False


@pytest.mark.django_db
def test_remove_tablet_requires_reauth(client, tablet_ui_scope):
    scope = tablet_ui_scope
    tablet = _pending_tablet(scope.department)
    client.force_login(scope.admin)

    response = client.post(
        reverse("tablet-remove", args=(scope.department.id, tablet.id)),
        {"status": Tablet.Status.LOST, "reason": ""},
    )
    assert response.status_code == 302
    assert "reauthenticate" in response.url
    tablet.refresh_from_db()
    assert tablet.status == Tablet.Status.PENDING
