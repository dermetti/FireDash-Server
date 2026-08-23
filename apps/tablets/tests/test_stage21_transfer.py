"""Stage 2.1 transfer semantics regression tests (assignment change only)."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.services import assign_tablet_vehicle
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def transfer_scope(db):
    admin = User.objects.create_user("transfer@example.test", "Transfer", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station_a = Station.objects.create(department=department, name="A", short_code="SA")
    station_b = Station.objects.create(department=department, name="B", short_code="SB")
    vehicle_a = Vehicle.objects.create(department=department, station=station_a, display_name="A1")
    vehicle_b = Vehicle.objects.create(department=department, station=station_b, display_name="B1")
    return SimpleNamespace(
        admin=admin,
        department=department,
        station_a=station_a,
        station_b=station_b,
        vehicle_a=vehicle_a,
        vehicle_b=vehicle_b,
    )


def _installation(tablet, status=AppInstallation.Status.ACTIVE):
    now = timezone.now()
    return AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash="a" * 64,
        status=status,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"public",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=7),
    )


def _current_vehicle(tablet):
    assignment = tablet.vehicle_assignments.filter(
        valid_until__isnull=True, ended_at__isnull=True
    ).first()
    return assignment.vehicle if assignment else None


@pytest.mark.django_db
def test_transfer_does_not_create_or_replace_installation(transfer_scope):
    scope = transfer_scope
    tablet = Tablet.objects.create(
        department=scope.department, display_name="FD-014", status=Tablet.Status.ACTIVE
    )
    installation = _installation(tablet)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    assert _current_vehicle(tablet) == scope.vehicle_a

    # Transfer to another station in the same department.
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_b, actor=scope.admin)

    # Same installation, unchanged and still ACTIVE; no new installation created.
    assert AppInstallation.objects.filter(tablet=tablet).count() == 1
    installation.refresh_from_db()
    assert installation.status == AppInstallation.Status.ACTIVE
    # New assignment/scope reflected using the same installation.
    assert _current_vehicle(tablet) == scope.vehicle_b
    assert tablet.vehicle_assignments.filter(
        ended_at__isnull=True, vehicle=scope.vehicle_b
    ).exists()


@pytest.mark.django_db
def test_transfer_does_not_change_asset_state(transfer_scope):
    scope = transfer_scope
    tablet = Tablet.objects.create(
        department=scope.department, display_name="FD-014", status=Tablet.Status.ACTIVE
    )
    _installation(tablet)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_b, actor=scope.admin)
    tablet.refresh_from_db()
    assert tablet.status == Tablet.Status.ACTIVE


@pytest.mark.django_db
def test_assign_view_uses_transfer_wording_when_assigned(client, transfer_scope):
    scope = transfer_scope
    tablet = Tablet.objects.create(
        department=scope.department, display_name="FD-014", status=Tablet.Status.ACTIVE
    )
    assign_tablet_vehicle(tablet=tablet, vehicle=scope.vehicle_a, actor=scope.admin)
    client.force_login(scope.admin)
    response = client.get(reverse("tablet-detail", args=(scope.department.id, tablet.id)))
    html = response.content.decode()
    assert "Transfer" in html
    assert "Move" not in html
