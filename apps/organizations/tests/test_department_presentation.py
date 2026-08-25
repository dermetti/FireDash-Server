from datetime import UTC, datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.authorization.services import set_department_locale_time_policy
from apps.organizations.models import Department, Station
from apps.organizations.presentation import format_department_datetime
from apps.tablets.services import canonical_protocol_datetime


@pytest.fixture
def presentation_scope(db):
    admin = User.objects.create_user("presentation-admin@example.test", "Admin", "password")
    other_admin = User.objects.create_user("presentation-other@example.test", "Other", "password")
    station_admin = User.objects.create_user(
        "presentation-station@example.test", "Station", "password"
    )
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other_department = Department.objects.create(
        name="Bravo", short_code="BRV", created_by=other_admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(
        user=other_admin, department=other_department, created_by=other_admin
    )
    station = Station.objects.create(department=department, name="Alpha One", short_code="A1")
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    return admin, other_admin, station_admin, department, other_department


@pytest.mark.django_db
def test_department_presentation_defaults_are_german_berlin(presentation_scope):
    _, _, _, department, _ = presentation_scope
    assert department.locale == "de-DE"
    assert department.timezone == "Europe/Berlin"


@pytest.mark.django_db
def test_locale_time_policy_is_scoped_validated_and_audited(presentation_scope):
    admin, other_admin, station_admin, department, _ = presentation_scope
    changed = set_department_locale_time_policy(
        actor=admin, department=department, locale="en-GB", timezone_name="Europe/London"
    )
    assert (changed.locale, changed.timezone) == ("en-GB", "Europe/London")
    assert AuditEvent.objects.filter(
        action="authorization.department_locale_time_policy_changed", department=department
    ).exists()
    with pytest.raises(PermissionDenied):
        set_department_locale_time_policy(
            actor=other_admin, department=department, locale="de-DE", timezone_name="Europe/Berlin"
        )
    with pytest.raises(PermissionDenied):
        set_department_locale_time_policy(
            actor=station_admin,
            department=department,
            locale="de-DE",
            timezone_name="Europe/Berlin",
        )
    with pytest.raises(ValueError, match="Unsupported Department locale"):
        set_department_locale_time_policy(
            actor=admin, department=department, locale="en-US", timezone_name="Europe/Berlin"
        )
    with pytest.raises(ValueError, match="Unsupported Department timezone"):
        set_department_locale_time_policy(
            actor=admin, department=department, locale="de-DE", timezone_name="America/New_York"
        )


@pytest.mark.django_db
def test_department_datetime_uses_iana_dst_and_does_not_leak_between_departments(
    presentation_scope,
):
    admin, _, _, berlin, london = presentation_scope
    set_department_locale_time_policy(
        actor=admin, department=berlin, locale="en-GB", timezone_name="Europe/Berlin"
    )
    london.timezone = "Europe/London"
    london.locale = "en-GB"
    london.save(update_fields=("locale", "timezone"))
    winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, tzinfo=UTC)
    assert format_department_datetime(winter, berlin).endswith("13:00")
    assert format_department_datetime(summer, berlin).endswith("14:00")
    assert format_department_datetime(summer, london).endswith("13:00")
    assert format_department_datetime(summer, berlin).endswith("14:00")


@pytest.mark.django_db
def test_locale_policy_does_not_change_canonical_protocol_timestamp(presentation_scope):
    admin, _, _, department, _ = presentation_scope
    timestamp = datetime(2026, 7, 15, 12, tzinfo=UTC)
    before = canonical_protocol_datetime(timestamp)
    set_department_locale_time_policy(
        actor=admin, department=department, locale="en-GB", timezone_name="Europe/London"
    )
    assert canonical_protocol_datetime(timestamp) == before == "2026-07-15T12:00:00Z"


@pytest.mark.django_db
def test_locale_time_settings_card_is_authorized_and_preserves_other_policy(
    client, presentation_scope
):
    admin, _, station_admin, department, _ = presentation_scope
    client.force_login(admin)
    url = reverse("portal-department-settings", args=(department.id,))
    response = client.get(url)
    content = response.content.decode()
    assert "Locale and time display" in content
    assert "Europe/Berlin" in content and "de-DE" in content
    assert "Publication retention" not in content
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    response = client.post(
        url,
        {"action": "locale-time", "locale": "en-GB", "timezone": "Europe/London"},
    )
    assert response.status_code == 302
    department.refresh_from_db()
    assert (department.locale, department.timezone, department.tablet_lease_days) == (
        "en-GB",
        "Europe/London",
        7,
    )
    client.force_login(station_admin)
    assert (
        client.post(
            url,
            {"action": "locale-time", "locale": "de-DE", "timezone": "Europe/Berlin"},
        ).status_code
        == 403
    )
