"""Stage 2.1 tablet list regression tests (single list, asset states, filters)."""

from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def list_scope(db):
    admin = User.objects.create_user("list@example.test", "List", "safe-password")
    other_admin = User.objects.create_user("other@example.test", "Other", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other = Department.objects.create(name="Bravo", short_code="BRV", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(user=other_admin, department=other, created_by=admin)
    station_a = Station.objects.create(department=department, name="Station A", short_code="SA")
    station_b = Station.objects.create(department=department, name="Station B", short_code="SB")
    vehicle_a = Vehicle.objects.create(department=department, station=station_a, display_name="A1")
    vehicle_b = Vehicle.objects.create(department=department, station=station_b, display_name="B1")
    return SimpleNamespace(
        admin=admin,
        other_admin=other_admin,
        department=department,
        other=other,
        station_a=station_a,
        station_b=station_b,
        vehicle_a=vehicle_a,
        vehicle_b=vehicle_b,
    )


def _tablet(department, name, status=Tablet.Status.ACTIVE):
    return Tablet.objects.create(department=department, display_name=name, status=status)


def _names(response):
    return [t.display_name for t in response.context["tablets"]]


@pytest.mark.django_db
def test_list_renders_single_results_table(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "active", Tablet.Status.ACTIVE)
    _tablet(scope.department, "retired", Tablet.Status.RETIRED)
    client.force_login(scope.admin)
    response = client.get(reverse("tablet-list", args=(scope.department.id,)))
    html = response.content.decode()
    assert html.count("<table") == 1


@pytest.mark.django_db
def test_default_ordering_is_active_first_and_deterministic(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "lost", Tablet.Status.LOST)
    _tablet(scope.department, "active-b", Tablet.Status.ACTIVE)
    _tablet(scope.department, "inactive", Tablet.Status.INACTIVE)
    _tablet(scope.department, "active-a", Tablet.Status.ACTIVE)
    client.force_login(scope.admin)
    response = client.get(reverse("tablet-list", args=(scope.department.id,)))
    names = _names(response)
    assert names[0] == "active-a"
    assert names[1] == "active-b"
    assert names[2] == "inactive"
    assert names[3] == "lost"
    again = _names(client.get(reverse("tablet-list", args=(scope.department.id,))))
    assert again == names


@pytest.mark.django_db
def test_default_includes_operational_and_excludes_historical(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "active", Tablet.Status.ACTIVE)
    _tablet(scope.department, "inactive", Tablet.Status.INACTIVE)
    _tablet(scope.department, "lost", Tablet.Status.LOST)
    _tablet(scope.department, "retired", Tablet.Status.RETIRED)
    client.force_login(scope.admin)
    names = _names(client.get(reverse("tablet-list", args=(scope.department.id,))))
    assert set(names) == {"active", "inactive", "lost"}


@pytest.mark.django_db
def test_status_filter_exposes_historical_rows(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "retired", Tablet.Status.RETIRED)
    _tablet(scope.department, "active", Tablet.Status.ACTIVE)
    client.force_login(scope.admin)
    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"status": "RETIRED"}
    )
    assert _names(response) == ["retired"]


@pytest.mark.django_db
def test_installation_health_filter(client, list_scope):
    scope = list_scope
    stale_tablet = _tablet(scope.department, "stale-install", Tablet.Status.ACTIVE)
    current_tablet = _tablet(scope.department, "current-install", Tablet.Status.ACTIVE)
    _tablet(scope.department, "no-install", Tablet.Status.ACTIVE)
    now = timezone.now()
    for tablet in (stale_tablet, current_tablet):
        install_status = (
            AppInstallation.Status.STALE
            if tablet is stale_tablet
            else AppInstallation.Status.ACTIVE
        )
        AppInstallation.objects.create(
            tablet=tablet,
            installation_uuid=tablet.id,
            credential_hash="a" * 64,
            status=install_status,
            app_version="1.0.0",
            adopted_app_version="1.0.0",
            app_version_seen_at=now,
            hpke_public_key=b"public",
            hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
            hpke_key_fingerprint="b" * 64,
            hpke_key_verified_at=now,
            adopted_at=now,
            authorization_valid_until=now,
        )
    client.force_login(scope.admin)
    stale = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"installation": "stale"}
    )
    assert _names(stale) == ["stale-install"]
    current = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"installation": "current"}
    )
    assert _names(current) == ["current-install"]
    none = client.get(reverse("tablet-list", args=(scope.department.id,)), {"installation": "none"})
    assert _names(none) == ["no-install"]


@pytest.mark.django_db
def test_station_filter_is_data_only(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "on-a", Tablet.Status.ACTIVE)
    _tablet(scope.department, "on-b", Tablet.Status.ACTIVE)
    a = Tablet.objects.get(display_name="on-a")
    b = Tablet.objects.get(display_name="on-b")
    TabletVehicleAssignment.objects.create(
        tablet=a, vehicle=scope.vehicle_a, valid_from=timezone.now(), created_by=scope.admin
    )
    TabletVehicleAssignment.objects.create(
        tablet=b, vehicle=scope.vehicle_b, valid_from=timezone.now(), created_by=scope.admin
    )
    client.force_login(scope.admin)
    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"station": str(scope.station_a.id)}
    )
    assert _names(response) == ["on-a"]


@pytest.mark.django_db
def test_bounded_pagination(client, list_scope):
    scope = list_scope
    for i in range(105):
        _tablet(scope.department, f"tablet-{i:03d}", Tablet.Status.ACTIVE)
    client.force_login(scope.admin)
    response = client.get(reverse("tablet-list", args=(scope.department.id,)))
    page = response.context["page"]
    assert page.paginator.per_page == 100
    assert len(response.context["tablets"]) == 100
    assert page.has_next() is True
    page2 = client.get(reverse("tablet-list", args=(scope.department.id,)), {"page": 2})
    assert len(page2.context["tablets"]) == 5


@pytest.mark.django_db
def test_pagination_preserves_filters(client, list_scope):
    scope = list_scope
    for i in range(105):
        _tablet(scope.department, f"active-{i:03d}", Tablet.Status.ACTIVE)
    _tablet(scope.department, "retired", Tablet.Status.RETIRED)
    client.force_login(scope.admin)
    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"status": "ACTIVE", "page": 2}
    )
    assert "status=ACTIVE" in response.context["page_query"]
    assert "page" not in response.context["page_query"]


@pytest.mark.django_db
def test_no_cross_department_leakage(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "alpha", Tablet.Status.ACTIVE)
    _tablet(scope.other, "bravo", Tablet.Status.ACTIVE)
    client.force_login(scope.other_admin)
    response = client.get(reverse("tablet-list", args=(scope.other.id,)))
    assert _names(response) == ["bravo"]


@pytest.mark.django_db
def test_explicit_sort_is_respected(client, list_scope):
    scope = list_scope
    _tablet(scope.department, "zeta", Tablet.Status.ACTIVE)
    _tablet(scope.department, "alpha", Tablet.Status.ACTIVE)
    client.force_login(scope.admin)
    response = client.get(
        reverse("tablet-list", args=(scope.department.id,)), {"sort": "name", "dir": "asc"}
    )
    assert _names(response) == ["alpha", "zeta"]
