import uuid
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import (
    ApiVersionCompatibilityPolicy,
    DepartmentMembership,
    StationAdminAssignment,
    SystemRole,
    VehicleRescueGuidesConfiguration,
)
from apps.organizations.models import Department, Station
from apps.portal.overview import department_attention
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob


@pytest.fixture
def system_scope(db):
    system_admin = User.objects.create_user("system@example.test", "System", "safe-password")
    department_admin = User.objects.create_user(
        "department@example.test", "Department", "safe-password"
    )
    station_admin = User.objects.create_user("station@example.test", "Station", "safe-password")
    SystemRole.objects.create(user=system_admin)
    department = Department.objects.create(
        name="Alpha Department", short_code="ALP", created_by=system_admin
    )
    station = Station.objects.create(department=department, name="Alpha Station", short_code="AS1")
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=system_admin
    )
    StationAdminAssignment.objects.create(
        user=station_admin, station=station, created_by=department_admin
    )
    return system_admin, department_admin, station_admin, department, station


def _reauthenticate(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


@pytest.mark.django_db
def test_system_navigation_is_isolated_and_all_roles_keep_their_scoped_entries(
    client, system_scope
):
    system_admin, department_admin, station_admin, department, _ = system_scope
    client.force_login(system_admin)
    response = client.get(reverse("dashboard"))
    content = response.content.decode()
    for label in (
        "Departments",
        "System Data Hub",
        "API Compatibility",
        "System Settings",
        "Audit / System Events",
    ):
        assert label in content
    for forbidden in ("Hydrants", "Fire Plans", "KLGV", "Tablets"):
        assert forbidden not in content
    client.force_login(department_admin)
    content = client.get(reverse("tablet-list", args=(department.id,))).content.decode()
    assert "Data Hub" in content and "Administrator Accounts" in content
    assert "Hydrants" not in content and "API Compatibility" not in content
    client.force_login(station_admin)
    content = client.get(reverse("dashboard")).content.decode()
    assert "Personnel" in content and "Tablets" in content
    assert "Data Hub" not in content and "Administrator Accounts" not in content


@pytest.mark.django_db
def test_system_search_lists_use_personnel_live_contract_and_return_only_results(
    client, system_scope
):
    system_admin, _, _, department, _ = system_scope
    client.force_login(system_admin)
    for template, target in (
        ("templates/portal/system_departments.html", "#system-department-results"),
        ("templates/portal/system_audit.html", "#system-audit-results"),
    ):
        content = (Path.cwd() / template).read_text(encoding="utf-8")
        assert "input changed delay:1s" in content
        assert target in content
        assert "hx-include" in content and "hx-push-url" in content

    response = client.get(
        reverse("portal-system-departments"), {"q": department.name}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert 'id="system-department-results"' in response.content.decode()
    assert "<!DOCTYPE" not in response.content.decode()


@pytest.mark.django_db
def test_system_departments_is_bounded_deterministic_filterable_and_system_only(
    client, system_scope
):
    system_admin, department_admin, _, department, _ = system_scope
    for index in range(101):
        Department.objects.create(
            name=f"Department {index:03d}", short_code=f"D{index:03d}", created_by=system_admin
        )
    client.force_login(system_admin)
    response = client.get(reverse("portal-system-departments"))
    assert response.status_code == 200
    page = response.context["page"]
    assert len(response.context["departments"]) == 100
    assert page.has_next
    assert list(response.context["departments"].values_list("name", flat=True)) == sorted(
        response.context["departments"].values_list("name", flat=True)
    )
    content = response.content.decode()
    assert (
        "<table" in content
        and reverse("portal-system-department", args=(department.id,)) in content
    )
    filtered = client.get(reverse("portal-system-departments"), {"q": department.short_code})
    assert list(filtered.context["departments"]) == [department]
    client.force_login(department_admin)
    assert client.get(reverse("portal-system-departments")).status_code == 403


@pytest.mark.django_db
def test_system_department_detail_uses_existing_audited_lifecycle_and_lease_services(
    client, system_scope
):
    system_admin, _, _, department, _ = system_scope
    client.force_login(system_admin)
    _reauthenticate(client)
    response = client.post(
        reverse("portal-system-department", args=(department.id,)),
        {"action": "tablet-lease", "tablet_lease_days": 14},
    )
    assert response.status_code == 302
    department.refresh_from_db()
    assert department.tablet_lease_days == 14
    assert AuditEvent.objects.filter(
        action="authorization.department_tablet_lease_changed", target_uuid=department.id
    ).exists()


@pytest.mark.django_db
def test_api_compatibility_is_structured_modal_validated_audited_and_system_only(
    client, system_scope
):
    system_admin, department_admin, _, _, _ = system_scope
    client.force_login(system_admin)
    page = client.get(reverse("portal-system-api-compatibility"))
    assert page.status_code == 200
    assert "Compatibility policies" in page.content.decode()
    modal_url = reverse("portal-system-api-compatibility-edit", args=(1,))
    modal = client.get(modal_url, HTTP_HX_REQUEST="true")
    assert modal.status_code == 200
    assert 'class="modal fade"' in modal.content.decode()
    invalid = client.post(
        modal_url, {"minimum_app_version": "not-a-version"}, HTTP_HX_REQUEST="true"
    )
    assert invalid.status_code == 200
    assert "not-a-version" in invalid.content.decode()
    _reauthenticate(client)
    saved = client.post(modal_url, {"minimum_app_version": "1.2.3"}, HTTP_HX_REQUEST="true")
    assert saved.status_code == 200
    assert saved["HX-Redirect"] == reverse("portal-system-api-compatibility")
    assert ApiVersionCompatibilityPolicy.objects.get(api_major=1).minimum_app_version == "1.2.3"
    assert AuditEvent.objects.filter(action="api_compatibility_policy.updated").exists()
    client.force_login(department_admin)
    assert client.get(modal_url).status_code == 403


@pytest.mark.django_db
def test_system_audit_is_bounded_newest_first_filtered_safe_and_system_only(client, system_scope):
    system_admin, department_admin, _, department, _ = system_scope
    for index in range(101):
        AuditEvent.objects.create(
            actor_user=system_admin,
            department=department,
            action=f"system.test_{index:03d}",
            target_type="test",
            target_uuid=uuid.uuid4(),
            request_id=uuid.uuid4(),
            metadata={"secret": "must-not-render"},
        )
    client.force_login(system_admin)
    response = client.get(reverse("portal-system-audit"), {"department": department.id})
    assert response.status_code == 200
    assert len(response.context["events"]) == 100
    events = list(response.context["events"])
    assert events[0].action == "system.test_100"
    content = response.content.decode()
    assert "Safe event context recorded" in content
    assert "must-not-render" not in content
    filtered = client.get(reverse("portal-system-audit"), {"action": "system.test_100"})
    assert filtered.context["total_count"] == 1
    client.force_login(department_admin)
    assert client.get(reverse("portal-system-audit")).status_code == 403


@pytest.mark.django_db
def test_system_settings_only_exposes_existing_supported_policies_and_is_system_only(
    client, system_scope
):
    system_admin, department_admin, _, _, _ = system_scope
    client.force_login(system_admin)
    response = client.get(reverse("portal-system-settings"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "API Compatibility" in content
    assert "Backups" not in content and "System Health" not in content
    client.force_login(department_admin)
    assert client.get(reverse("portal-system-settings")).status_code == 403


@pytest.mark.django_db
def test_api_compatibility_mutation_requires_post_and_csrf(system_scope):
    system_admin, _, _, _, _ = system_scope
    url = reverse("portal-system-api-compatibility-edit", args=(1,))
    client = Client(enforce_csrf_checks=True)
    client.force_login(system_admin)
    assert client.get(url).status_code == 200
    assert client.post(url, {"minimum_app_version": "1.2.3"}).status_code == 403


@pytest.mark.django_db
def test_system_data_hub_vehicle_rescue_guides_is_system_only_and_uses_audited_service(
    client, system_scope
):
    system_admin, department_admin, _, department, _ = system_scope
    hub_url = reverse("portal-system-data-hub")
    configuration_url = reverse("portal-system-vehicle-rescue-guides")

    client.force_login(system_admin)
    hub = client.get(hub_url)
    content = hub.content.decode()
    assert hub.status_code == 200
    assert "Vehicle Rescue Guides" in content
    assert "Enabled" in content and "Euro RESCUE" in content and "Web" in content
    assert "https://rescue.euroncap.com/" in content
    assert configuration_url in content

    invalid = client.post(configuration_url, {"web_url": "http://example.test"})
    assert invalid.status_code == 200
    assert "absolute HTTPS" in invalid.content.decode()
    assert VehicleRescueGuidesConfiguration.objects.count() == 1

    _reauthenticate(client)
    saved = client.post(configuration_url, {"web_url": "https://rescue.euroncap.com/app?locale=de"})
    assert saved.status_code == 302
    assert saved.url == configuration_url
    assert VehicleRescueGuidesConfiguration.objects.get().web_url.endswith("locale=de")
    assert AuditEvent.objects.filter(action="vehicle_rescue_guides_configuration.updated").exists()
    assert DatasetPublication.objects.count() == 0
    assert DatasetScopeState.objects.filter(department=department).count() == 0
    assert PublicationJob.objects.count() == 0
    assert department_attention(department) == []

    client.force_login(department_admin)
    assert client.get(hub_url).status_code == 403
    assert client.get(configuration_url).status_code == 403


@pytest.mark.django_db
def test_department_data_hub_renders_vehicle_rescue_guides_as_read_only_system_configuration(
    client, system_scope
):
    _, department_admin, _, department, _ = system_scope
    client.force_login(department_admin)

    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    content = response.content.decode()

    assert response.status_code == 200
    assert "System-provided configuration" in content
    assert "Vehicle Rescue Guides" in content
    assert "Enabled" in content and "Euro RESCUE" in content and "Web" in content
    assert "System managed" in content
    assert "https://rescue.euroncap.com/" in content
    assert "Save web application URL" not in content
    assert "Publish" not in content and "Rollback" not in content and "Build" not in content
    assert VehicleRescueGuidesConfiguration.objects.count() == 1
    assert DatasetPublication.objects.count() == 0
    assert DatasetScopeState.objects.filter(department=department).count() == 0
    assert PublicationJob.objects.count() == 0
