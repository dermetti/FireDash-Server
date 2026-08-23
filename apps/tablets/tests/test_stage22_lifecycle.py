"""Stage 2.2 Tablet asset and installation lifecycle hardening tests."""

import hashlib
import hmac
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import (
    TabletError,
    activate_tablet,
    check_in,
    complete_adoption,
    create_adoption_invitation,
    create_adoption_request,
    deactivate_tablet,
    generate_credential,
    mark_tablet_lost,
    recover_tablet,
)

from .conftest import _p256_public_key


@pytest.fixture
def stage22_scope(db):
    user = User.objects.create_user("stage22@example.test", "Stage 22", "safe-password")
    department = Department.objects.create(name="Stage 22", short_code="S22", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="S22")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    return user, department, vehicle


def _tablet(*, department, status=Tablet.Status.INACTIVE, name="Tablet"):
    return Tablet.objects.create(department=department, display_name=name, status=status)


def _assign(*, tablet, vehicle, actor):
    return TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=actor
    )


def _installation(*, tablet, status=AppInstallation.Status.ACTIVE, expiry=None):
    now = timezone.now()
    credential = generate_credential()
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=status,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"public",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=expiry or now + timedelta(days=3),
    )
    return installation, credential


@pytest.mark.django_db
def test_stale_current_installation_auto_recovers_through_check_in(stage22_scope):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department, status=Tablet.Status.ACTIVE)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    installation, credential = _installation(
        tablet=tablet,
        status=AppInstallation.Status.STALE,
        expiry=timezone.now() - timedelta(minutes=1),
    )
    installation.stale_at = timezone.now() - timedelta(minutes=1)
    installation.save(update_fields=("stale_at",))

    recovered = check_in(installation=installation, credential=credential)

    recovered.refresh_from_db()
    assert recovered.id == installation.id
    assert recovered.status == AppInstallation.Status.ACTIVE
    assert recovered.authorization_valid_until > timezone.now()
    assert recovered.last_successful_check_in_at is not None
    assert AppInstallation.objects.filter(tablet=tablet).count() == 1
    assert tablet.adoption_invitations.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tablet_status", "tablet_active", "installation_status"),
    [
        (Tablet.Status.LOST, False, AppInstallation.Status.STALE),
        (Tablet.Status.RETIRED, False, AppInstallation.Status.STALE),
        (Tablet.Status.ACTIVE, True, AppInstallation.Status.REVOKED),
        (Tablet.Status.ACTIVE, True, AppInstallation.Status.REPLACED),
    ],
)
def test_stale_or_terminal_installation_cannot_auto_recover(
    stage22_scope, tablet_status, tablet_active, installation_status
):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department, status=tablet_status)
    tablet.active = tablet_active
    tablet.save(update_fields=("active",))
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    installation, credential = _installation(
        tablet=tablet,
        status=installation_status,
        expiry=timezone.now() - timedelta(minutes=1),
    )

    with pytest.raises(
        TabletError, match="not active|not active for operational service|department must be active"
    ):
        check_in(installation=installation, credential=credential)

    installation.refresh_from_db()
    assert installation.status == installation_status


@pytest.mark.django_db
def test_inactive_stale_installation_can_only_check_in_for_control_plane(stage22_scope):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department, status=Tablet.Status.INACTIVE)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    installation, credential = _installation(
        tablet=tablet,
        status=AppInstallation.Status.STALE,
        expiry=timezone.now() - timedelta(minutes=1),
    )

    checked_in = check_in(installation=installation, credential=credential)

    assert checked_in.status == AppInstallation.Status.STALE
    assert checked_in.last_successful_check_in_at is not None
    assert checked_in.authorization_valid_until <= timezone.now()


@pytest.mark.django_db
def test_deactivation_keeps_current_installation_and_reactivation_reuses_it(stage22_scope):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department, status=Tablet.Status.ACTIVE)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    installation, _ = _installation(tablet=tablet)

    deactivated = deactivate_tablet(actor=user, tablet=tablet, reason="Workshop")
    installation.refresh_from_db()
    assert deactivated.status == Tablet.Status.INACTIVE
    assert installation.status == AppInstallation.Status.ACTIVE

    reactivated = activate_tablet(actor=user, tablet=tablet)
    installation.refresh_from_db()
    assert reactivated.status == Tablet.Status.ACTIVE
    assert installation.status == AppInstallation.Status.ACTIVE
    assert AppInstallation.objects.filter(tablet=tablet).count() == 1


@pytest.mark.django_db
def test_adoption_of_inactive_tablet_does_not_commission_the_asset(stage22_scope):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=_p256_public_key(),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )

    complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )

    tablet.refresh_from_db()
    assert tablet.status == Tablet.Status.INACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "installation_status", [None, AppInstallation.Status.REVOKED, AppInstallation.Status.REPLACED]
)
def test_activation_requires_current_operational_installation(stage22_scope, installation_status):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    if installation_status is not None:
        _installation(tablet=tablet, status=installation_status)

    with pytest.raises(TabletError, match="current installation"):
        activate_tablet(actor=user, tablet=tablet)


@pytest.mark.django_db
def test_activation_requires_current_assignment_and_rejects_cross_department_assignment(
    stage22_scope,
):
    user, department, _ = stage22_scope
    tablet = _tablet(department=department)
    _installation(tablet=tablet)

    with pytest.raises(TabletError, match="vehicle assignment"):
        activate_tablet(actor=user, tablet=tablet)

    other_department = Department.objects.create(name="Other", short_code="OTH", created_by=user)
    other_station = Station.objects.create(
        department=other_department, name="Other", short_code="OTH"
    )
    other_vehicle = Vehicle.objects.create(
        department=other_department, station=other_station, display_name="Other Engine"
    )
    _assign(tablet=tablet, vehicle=other_vehicle, actor=user)

    with pytest.raises(TabletError, match="vehicle assignment"):
        activate_tablet(actor=user, tablet=tablet)


@pytest.mark.django_db
def test_lost_recovery_does_not_resurrect_revoked_installation(stage22_scope):
    user, department, vehicle = stage22_scope
    tablet = _tablet(department=department, status=Tablet.Status.ACTIVE)
    _assign(tablet=tablet, vehicle=vehicle, actor=user)
    installation, credential = _installation(tablet=tablet)

    mark_tablet_lost(actor=user, tablet=tablet, reason="Missing")
    recover_tablet(actor=user, tablet=tablet)

    tablet.refresh_from_db()
    installation.refresh_from_db()
    assert tablet.status == Tablet.Status.INACTIVE
    assert installation.status == AppInstallation.Status.REVOKED
    with pytest.raises(TabletError, match="not active"):
        check_in(installation=installation, credential=credential)


@pytest.mark.django_db
def test_database_rejects_removed_asset_state(stage22_scope):
    _, department, _ = stage22_scope
    with pytest.raises(IntegrityError), transaction.atomic():
        Tablet.objects.create(department=department, display_name="Legacy", status="REMOVED")
