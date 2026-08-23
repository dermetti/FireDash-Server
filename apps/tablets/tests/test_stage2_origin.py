"""Stage 2.1 originating-page return behavior regression tests."""

from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import Tablet


@pytest.fixture
def origin_scope(db):
    admin = User.objects.create_user("origin@example.test", "Origin", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    return SimpleNamespace(admin=admin, department=department, station=station, vehicle=vehicle)


def _login_with_reauth(client, user):
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


@pytest.mark.django_db
def test_list_origin_returns_to_list_and_preserves_query(client, origin_scope):
    scope = origin_scope
    tablet = Tablet.objects.create(department=scope.department, display_name="Tablet")
    _login_with_reauth(client, scope.admin)
    list_url = reverse("tablet-list", args=(scope.department.id,)) + "?status=ACTIVE"
    response = client.post(
        reverse("tablet-mark-lost", args=(scope.department.id, tablet.id)),
        {"reason": "", "next": list_url},
    )
    assert response.status_code == 302
    assert response.url == list_url


@pytest.mark.django_db
def test_list_origin_shows_feedback_on_list(client, origin_scope):
    scope = origin_scope
    tablet = Tablet.objects.create(department=scope.department, display_name="Tablet")
    _login_with_reauth(client, scope.admin)
    list_url = reverse("tablet-list", args=(scope.department.id,)) + "?status=ACTIVE"
    response = client.post(
        reverse("tablet-mark-lost", args=(scope.department.id, tablet.id)),
        {"reason": "", "next": list_url},
        follow=True,
    )
    assert "marked lost" in response.content.decode()


@pytest.mark.django_db
def test_detail_origin_returns_to_detail(client, origin_scope):
    scope = origin_scope
    tablet = Tablet.objects.create(department=scope.department, display_name="Tablet")
    _login_with_reauth(client, scope.admin)
    detail_url = reverse("tablet-detail", args=(scope.department.id, tablet.id))
    response = client.post(
        reverse("tablet-mark-lost", args=(scope.department.id, tablet.id)),
        {"reason": ""},
    )
    assert response.status_code == 302
    assert response.url == detail_url


@pytest.mark.django_db
def test_external_return_target_is_rejected(client, origin_scope):
    scope = origin_scope
    tablet = Tablet.objects.create(department=scope.department, display_name="Tablet")
    _login_with_reauth(client, scope.admin)
    detail_url = reverse("tablet-detail", args=(scope.department.id, tablet.id))
    response = client.post(
        reverse("tablet-mark-lost", args=(scope.department.id, tablet.id)),
        {"reason": "", "next": "https://evil.example/phish"},
    )
    assert response.status_code == 302
    assert response.url == detail_url
