"""Stage 2.1 physical Tablet asset lifecycle regression tests (PostgreSQL-backed)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import (
    TabletError,
    activate_tablet,
    deactivate_tablet,
    mark_tablet_lost,
    recover_tablet,
    retire_tablet,
)


@pytest.fixture
def lifecycle_scope(db):
    admin = User.objects.create_user("lifecycle@example.test", "Lifecycle", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    return SimpleNamespace(admin=admin, department=department, station=station, vehicle=vehicle)


def _tablet(department, status=Tablet.Status.INACTIVE, name="Tablet"):
    return Tablet.objects.create(department=department, display_name=name, status=status)


def _current_installation(tablet):
    now = timezone.now()
    return AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash="a" * 64,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"public",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )


def _login_with_reauth(client, user):
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


@pytest.mark.django_db
def test_inactive_to_active(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.INACTIVE)
    from apps.assignments.models import TabletVehicleAssignment

    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=scope.vehicle, valid_from=timezone.now(), created_by=scope.admin
    )
    _current_installation(tablet)
    activated = activate_tablet(actor=scope.admin, tablet=tablet)
    assert activated.status == Tablet.Status.ACTIVE
    assert activated.active is True


@pytest.mark.django_db
def test_active_to_inactive(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.ACTIVE)
    deactivated = deactivate_tablet(actor=scope.admin, tablet=tablet)
    assert deactivated.status == Tablet.Status.INACTIVE


@pytest.mark.django_db
def test_active_to_lost(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.ACTIVE)
    lost = mark_tablet_lost(actor=scope.admin, tablet=tablet)
    assert lost.status == Tablet.Status.LOST
    assert lost.active is False


@pytest.mark.django_db
def test_inactive_to_lost(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.INACTIVE)
    lost = mark_tablet_lost(actor=scope.admin, tablet=tablet)
    assert lost.status == Tablet.Status.LOST


@pytest.mark.django_db
def test_lost_to_inactive_recovery(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.LOST)
    recovered = recover_tablet(actor=scope.admin, tablet=tablet)
    assert recovered.status == Tablet.Status.INACTIVE
    assert recovered.active is True


@pytest.mark.django_db
def test_active_to_retired(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.ACTIVE)
    retired = retire_tablet(actor=scope.admin, tablet=tablet)
    assert retired.status == Tablet.Status.RETIRED
    assert retired.active is False


@pytest.mark.django_db
def test_inactive_to_retired(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.INACTIVE)
    retired = retire_tablet(actor=scope.admin, tablet=tablet)
    assert retired.status == Tablet.Status.RETIRED


@pytest.mark.django_db
def test_retired_is_terminal(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.RETIRED)
    for service in (
        lambda: activate_tablet(actor=scope.admin, tablet=tablet),
        lambda: deactivate_tablet(actor=scope.admin, tablet=tablet),
        lambda: mark_tablet_lost(actor=scope.admin, tablet=tablet),
        lambda: recover_tablet(actor=scope.admin, tablet=tablet),
        lambda: retire_tablet(actor=scope.admin, tablet=tablet),
    ):
        with pytest.raises(TabletError, match="not available"):
            service()


@pytest.mark.django_db
def test_lost_does_not_retire(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.LOST)
    with pytest.raises(TabletError, match="not available"):
        retire_tablet(actor=scope.admin, tablet=tablet)


@pytest.mark.django_db
def test_lost_does_not_activate_directly(lifecycle_scope):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.LOST)
    with pytest.raises(TabletError, match="not available"):
        activate_tablet(actor=scope.admin, tablet=tablet)


@pytest.mark.django_db
def test_tablet_asset_choices_exclude_removed(lifecycle_scope):
    assert {choice for choice, _ in Tablet.Status.choices} == {
        "INACTIVE",
        "ACTIVE",
        "LOST",
        "RETIRED",
    }


@pytest.mark.django_db
def test_recover_view(lifecycle_scope, client):
    scope = lifecycle_scope
    tablet = _tablet(scope.department, Tablet.Status.LOST)
    _login_with_reauth(client, scope.admin)
    response = client.post(reverse("tablet-recover", args=(scope.department.id, tablet.id)))
    assert response.status_code == 302
    tablet.refresh_from_db()
    assert tablet.status == Tablet.Status.INACTIVE
