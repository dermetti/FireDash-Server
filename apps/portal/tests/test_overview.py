"""Phase 4D Overview attention and operational-state regressions."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.organizations.models import Department, Station
from apps.portal.overview import _publication_attention, attention_for_request, department_attention
from apps.portal.views import _nav_context
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet


@pytest.fixture
def overview_scope(db):
    admin = User.objects.create_user("overview@example.test", "Overview", "password")
    department = Department.objects.create(name="Overview", short_code="OVW", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    return admin, department


def _item(items, text):
    return next(item for item in items if text in item.text)


@pytest.mark.django_db
def test_publication_attention_is_per_actionable_scope_with_reason_and_destination(overview_scope):
    _admin, department = overview_scope
    not_published = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    failed = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans", dirty_since=timezone.now()
    )
    DatasetPublication.objects.create(
        department=department,
        dataset_type_code=failed.dataset_type_code,
        scope_state=failed,
        version_number=1,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.FAILED,
    )
    scheduled = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_klgv_plans"
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scheduled.dataset_type_code,
        scope_state=scheduled,
        source_revision=1,
        status=PublicationJob.Status.PENDING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        not_before=timezone.now(),
    )

    items = department_attention(department)

    assert len([item for item in items if item.url.startswith("/publications/scopes/")]) == 2
    assert _item(items, "Not published").url == reverse(
        "publications-scope-detail", args=[not_published.id]
    )
    assert _item(items, "Update failed").url == reverse(
        "publications-scope-detail", args=[failed.id]
    )
    assert not any(str(scheduled.id) in item.url for item in items)


@pytest.mark.django_db(transaction=True)
def test_publication_attention_is_bounded_without_detailed_rows_or_source_rebuild(
    overview_scope, monkeypatch
):
    assert connection.vendor == "postgresql"
    _admin, department = overview_scope
    stations = Station.objects.bulk_create(
        [
            Station(department=department, name=f"Station {index}", short_code=f"S{index}")
            for index in range(400)
        ]
    )
    DatasetScopeState.objects.bulk_create(
        [
            DatasetScopeState(
                department=department, station=station, dataset_type_code="station_personnel"
            )
            for station in stations
        ]
    )

    def source_rebuild_called(*args, **kwargs):
        raise AssertionError("Overview must not rebuild canonical sources.")

    def detailed_rows_called(*args, **kwargs):
        raise AssertionError("Overview must not materialize detailed publication rows.")

    monkeypatch.setattr("apps.publications.builders.build_source_payload", source_rebuild_called)
    monkeypatch.setattr(
        "apps.publications.state.scope_operational_states_for_scopes", detailed_rows_called
    )
    with CaptureQueriesContext(connection) as queries:
        items = _publication_attention(department)

    assert len(items) == 400
    assert all(item.count == 1 for item in items)
    assert len(queries) <= 4
    sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
    # This attention path has no need for snapshot-retention state.  If that
    # later becomes useful, an ``IS NOT NULL`` annotation is still allowed;
    # the JSON payload itself must remain deferred.
    assert '"source_snapshot" as "source_snapshot"' not in sql


@pytest.mark.django_db
def test_tablet_adoption_and_attention_clear_without_cross_department_leakage(overview_scope):
    _admin, department = overview_scope
    tablet = Tablet.objects.create(department=department, display_name="Unassigned")
    now = timezone.now()
    AppInstallation.objects.create(
        tablet=tablet,
        status=AppInstallation.Status.STALE,
        installation_uuid=uuid.uuid4(),
        credential_hash="a" * 64,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"key",
        hpke_ciphersuite="test",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    Tablet.objects.create(department=department, display_name="Lost", status=Tablet.Status.LOST)
    AdoptionInvitation.objects.create(
        tablet=tablet,
        token_hash="c" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
        created_by=_admin,
    )
    outsider = User.objects.create_user("other-overview@example.test", "Other", "password")
    other = Department.objects.create(name="Other", short_code="OTH", created_by=outsider)
    Tablet.objects.create(department=other, display_name="Other lost", status=Tablet.Status.LOST)

    items = department_attention(department)

    assert _item(items, "unassigned").count == 1
    assert _item(items, "stale").count == 1
    assert _item(items, "marked lost").count == 1
    assert _item(items, "pending tablet adoption").count == 1
    assert sum(item.count for item in items) == 4

    tablet.status = Tablet.Status.RETIRED
    tablet.save(update_fields=("status",))
    AppInstallation.objects.filter(tablet=tablet).update(status=AppInstallation.Status.REVOKED)
    AdoptionInvitation.objects.filter(tablet=tablet).update(revoked_at=timezone.now())
    assert department_attention(department) == [
        _item(department_attention(department), "marked lost")
    ]


@pytest.mark.django_db
def test_overview_and_topbar_share_cached_role_scoped_attention(
    client, overview_scope, monkeypatch
):
    admin, department = overview_scope
    station_admin = User.objects.create_user("station-overview@example.test", "Station", "password")
    station = Station.objects.create(department=department, name="One", short_code="ONE")
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    calls = []

    from apps.portal import overview

    original = overview.department_attention

    def tracked(value):
        calls.append(value.id)
        return original(value)

    monkeypatch.setattr(overview, "department_attention", tracked)
    request = RequestFactory().get(reverse("dashboard"))
    request.user = admin
    context = _nav_context(request)
    assert attention_for_request(request, department=department) is context["nav_attention"]
    assert calls == [department.id]

    client.force_login(station_admin)
    content = client.get(reverse("dashboard")).content.decode()
    assert "No current attention items require action." in content
    overview_start = content.index("Requires attention")
    overview_end = content.index("Operational state", overview_start)
    overview_content = content[overview_start:overview_end]
    assert reverse("tablet-list", args=[department.id]) not in overview_content
    assert reverse("publications-list", args=[department.id]) not in overview_content


@pytest.mark.django_db
def test_dashboard_renders_attention_first_quiet_state_and_accessible_operational_text(
    client, overview_scope
):
    admin, department = overview_scope
    client.force_login(admin)
    quiet = client.get(reverse("dashboard")).content.decode()
    assert quiet.index("Requires attention") < quiet.index("Operational state")
    assert 'role="status"' in quiet
    assert "No current attention items require action." in quiet
    assert "active station(s)" in quiet

    DatasetScopeState.objects.create(department=department, dataset_type_code="department_hydrants")
    active = client.get(reverse("dashboard")).content.decode()
    assert "Not published" in active
    assert 'Attention <span class="badge text-bg-dark">1</span>' in active


@pytest.mark.django_db
def test_system_overview_uses_orphan_recovery_only(client, overview_scope):
    admin, _department = overview_scope
    system = User.objects.create_user("system-overview@example.test", "System", "password")
    SystemRole.objects.create(user=system)
    orphan = Department.objects.create(name="Orphan", short_code="ORP", created_by=admin)
    client.force_login(system)

    content = client.get(reverse("dashboard")).content.decode()

    assert "administrator recovery" in content
    assert reverse("portal-system-department", args=[orphan.id]) in content
    assert "System Health" not in content and "Backups" not in content
