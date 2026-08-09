import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def tablet_admin_data(db):
    department_admin = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    department = Department.objects.create(
        name="Alpha", short_code="ALP", created_by=department_admin
    )
    station = Station.objects.create(department=department, name="Alpha Station", short_code="AST")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    StationAdminAssignment.objects.create(
        user=station_admin, station=station, created_by=department_admin
    )
    return department_admin, station_admin, department


@pytest.mark.django_db
def test_department_admin_can_view_tablet_list_and_detail(client, tablet_admin_data):
    department_admin, _, department = tablet_admin_data
    tablet = Tablet.objects.create(
        department=department, display_name="Command Tablet", asset_number="TAB-1"
    )
    now = timezone.now()
    AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash="a" * 64,
        app_version="1.2.3",
        hpke_public_key=b"public-key",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    client.force_login(department_admin)

    assert client.get(reverse("tablet-list", args=(department.id,))).status_code == 200
    response = client.get(reverse("tablet-detail", args=(department.id, tablet.id)))
    assert response.status_code == 200
    assert "1.2.3" in response.content.decode()


@pytest.mark.django_db
def test_station_admin_is_denied_tablet_pages(client, tablet_admin_data):
    _, station_admin, department = tablet_admin_data
    tablet = Tablet.objects.create(department=department, display_name="Command Tablet")
    client.force_login(station_admin)

    assert client.get(reverse("tablet-list", args=(department.id,))).status_code == 403
    assert client.get(reverse("tablet-detail", args=(department.id, tablet.id))).status_code == 403


@pytest.mark.django_db
def test_department_admin_is_denied_cross_department_tablets(client, tablet_admin_data):
    department_admin, _, _ = tablet_admin_data
    other_department = Department.objects.create(
        name="Bravo", short_code="BRV", created_by=department_admin
    )
    tablet = Tablet.objects.create(department=other_department, display_name="Bravo Tablet")
    client.force_login(department_admin)

    assert client.get(reverse("tablet-list", args=(other_department.id,))).status_code == 403
    assert (
        client.get(reverse("tablet-detail", args=(other_department.id, tablet.id))).status_code
        == 403
    )


@pytest.mark.django_db
def test_tablet_list_filters_by_query_and_status(client, tablet_admin_data):
    department_admin, _, department = tablet_admin_data
    active = Tablet.objects.create(
        department=department,
        display_name="Command Tablet",
        asset_number="CMD-01",
        status=Tablet.Status.ACTIVE,
    )
    Tablet.objects.create(
        department=department,
        display_name="Reserve Tablet",
        asset_number="RSV-02",
        status=Tablet.Status.STALE,
    )
    client.force_login(department_admin)

    response = client.get(
        reverse("tablet-list", args=(department.id,)), {"q": "CMD", "status": "ACTIVE"}
    )

    assert list(response.context["tablets"]) == [active]
