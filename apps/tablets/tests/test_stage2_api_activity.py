"""Stage 2 tablet API activity diagnostics regression tests (PostgreSQL-backed)."""

import hashlib
import hmac
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.activity import (
    MAX_ACTIVITY_RECORDS_PER_INSTALLATION,
    prune_tablet_api_activity,
)
from apps.tablets.models import AppInstallation, Tablet, TabletApiActivity


@pytest.fixture
def activity_scope(db):
    now = timezone.now()
    admin = User.objects.create_user("activity@example.test", "Activity", "safe-password")
    other_admin = User.objects.create_user("otheract@example.test", "Other", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other = Department.objects.create(name="Bravo", short_code="BRV", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(user=other_admin, department=other, created_by=admin)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    Vehicle.objects.create(department=department, station=station, display_name="Engine")
    tablet = Tablet.objects.create(
        department=department, display_name="Tablet", status=Tablet.Status.ACTIVE
    )
    credential = "test-credential-value"
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"public",
        hpke_ciphersuite="DHKEM(P-256, HKDF-SHA256)",
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=7),
    )
    return SimpleNamespace(
        admin=admin,
        other_admin=other_admin,
        department=department,
        other=other,
        tablet=tablet,
        installation=installation,
        credential=credential,
    )


@pytest.mark.django_db
def test_status_request_records_safe_metadata(client, activity_scope):
    scope = activity_scope
    response = client.get(
        "/api/v1/tablet/status",
        HTTP_AUTHORIZATION=f"Bearer {scope.credential}",
    )
    assert response.status_code == 200
    activity = TabletApiActivity.objects.get(app_installation=scope.installation)
    assert activity.method == "GET"
    assert activity.path == "/api/v1/tablet/status"
    assert activity.status_code == 200


@pytest.mark.django_db
def test_query_string_is_not_stored(client, activity_scope):
    scope = activity_scope
    client.get(
        "/api/v1/tablet/status?token=super-secret-value",
        HTTP_AUTHORIZATION=f"Bearer {scope.credential}",
    )
    activity = TabletApiActivity.objects.get(app_installation=scope.installation)
    assert "super-secret-value" not in activity.path
    assert "?" not in activity.path


@pytest.mark.django_db
def test_no_secrets_are_stored(client, activity_scope):
    scope = activity_scope
    client.get(
        "/api/v1/tablet/status",
        HTTP_AUTHORIZATION=f"Bearer {scope.credential}",
    )
    activity = TabletApiActivity.objects.get(app_installation=scope.installation)
    assert scope.credential not in activity.path
    assert scope.credential not in str(activity.__dict__.values())


@pytest.mark.django_db
def test_activity_newest_twenty_across_installations(activity_scope):
    scope = activity_scope
    now = timezone.now()
    second = AppInstallation.objects.create(
        tablet=scope.tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash="b" * 64,
        status=AppInstallation.Status.REPLACED,
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
    for i in range(15):
        TabletApiActivity.objects.create(
            app_installation=scope.installation,
            occurred_at=now + timedelta(seconds=i),
            method="GET",
            path="/api/v1/tablet/status",
            status_code=200,
        )
        TabletApiActivity.objects.create(
            app_installation=second,
            occurred_at=now + timedelta(seconds=30 + i),
            method="GET",
            path="/api/v1/tablet/status",
            status_code=200,
        )
    result = list(
        TabletApiActivity.objects.filter(app_installation__tablet=scope.tablet).order_by(
            "-occurred_at"
        )[:20]
    )
    assert len(result) == 20
    assert result[0].app_installation_id == second.id
    timestamps = [item.occurred_at for item in result]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.django_db
def test_activity_view_scoped_to_department_admin(client, activity_scope):
    scope = activity_scope
    TabletApiActivity.objects.create(
        app_installation=scope.installation,
        occurred_at=timezone.now(),
        method="GET",
        path="/api/v1/tablet/status",
        status_code=200,
    )
    url = reverse("tablet-api-activity", args=(scope.department.id, scope.tablet.id))
    client.force_login(scope.admin)
    assert client.get(url).status_code == 200
    # A different department's admin cannot see this tablet's activity.
    client.force_login(scope.other_admin)
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_activity_partial_polls_every_three_seconds(client, activity_scope):
    scope = activity_scope
    client.force_login(scope.admin)
    url = reverse("tablet-api-activity", args=(scope.department.id, scope.tablet.id))
    html = client.get(url).content.decode()
    assert 'hx-trigger="every 3s"' in html
    assert 'hx-swap="outerHTML"' in html


@pytest.mark.django_db
def test_activity_retention_is_bounded(activity_scope):
    scope = activity_scope
    now = timezone.now()
    for i in range(MAX_ACTIVITY_RECORDS_PER_INSTALLATION + 10):
        TabletApiActivity.objects.create(
            app_installation=scope.installation,
            occurred_at=now + timedelta(seconds=i),
            method="GET",
            path="/api/v1/tablet/status",
            status_code=200,
        )
    deleted = prune_tablet_api_activity(now=now + timedelta(days=1))
    assert deleted == 10
    assert (
        TabletApiActivity.objects.filter(app_installation=scope.installation).count()
        == MAX_ACTIVITY_RECORDS_PER_INSTALLATION
    )
