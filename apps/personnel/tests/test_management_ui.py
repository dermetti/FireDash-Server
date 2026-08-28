import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.personnel.services import create_person
from apps.publications.models import PublicationJob


@pytest.fixture
def personnel_scope(client, db):
    admin = User.objects.create_user("person-admin@example.test", "Admin", "password")
    station_admin = User.objects.create_user("station-admin@example.test", "Station", "password")
    department = Department.objects.create(name="Personnel", short_code="PER", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Alpha", short_code="ALP")
    other_station = Station.objects.create(department=department, name="Bravo", short_code="BRV")
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    person = create_person(
        actor=admin,
        department=department,
        home_station=station,
        personnel_number="P-001",
        first_name="Ada",
        last_name="Lovelace",
    )
    client.force_login(admin)
    return admin, station_admin, department, station, other_station, person


def _payload(**overrides):
    data = {"personnel_number": "P-001", "first_name": "Grace", "last_name": "Hopper"}
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_personnel_list_is_bounded_active_default_filtered_and_scoped(client, personnel_scope):
    admin, station_admin, department, station, other_station, person = personnel_scope
    Person.objects.bulk_create(
        [
            Person(
                department=department,
                personnel_number=f"P-{index + 1000}",
                first_name="Person",
                last_name=f"{index:03}",
                display_name=f"Person {index:03}",
            )
            for index in range(101)
        ]
    )
    departed = Person.objects.create(
        department=department,
        display_name="Former member",
        lifecycle_status=Person.LifecycleStatus.DEPARTED,
        active=False,
        departed_at=timezone.now(),
    )
    response = client.get(reverse("personnel-list", args=(department.id,)))
    assert response.status_code == 200 and len(response.context["people"]) == 100
    assert response.context["total_count"] == 102
    body = response.content.decode()
    assert "table-responsive" not in body and "Page 1 of 2" in body
    assert reverse("personnel-detail", args=(department.id, person.id)) in body
    assert departed.display_name not in body
    departed_response = client.get(
        reverse("personnel-list", args=(department.id,)), {"status": "DEPARTED"}
    )
    assert list(departed_response.context["people"]) == [departed]

    client.force_login(station_admin)
    scoped = client.get(reverse("personnel-list", args=(department.id,)))
    assert scoped.status_code == 200
    assert list(scoped.context["people"]) == [person]
    assert other_station.name not in scoped.content.decode()
    client.force_login(admin)


@pytest.mark.django_db
def test_personnel_modal_edit_delete_protection_audit_and_dirtying(client, personnel_scope):
    _, _, department, station, _, person = personnel_scope
    detail_url = reverse("personnel-detail", args=(department.id, person.id))
    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert all(label in detail.content.decode() for label in ("Edit Data", "Delete Data"))
    detail_body = detail.content.decode()
    assert "person-action-modal-container" in detail_body
    assert 'data-bs-toggle="modal"' not in detail_body
    assert "htmx:afterSwap" in detail_body

    edit_url = reverse("personnel-edit", args=(department.id, person.id))
    edit_get = client.get(edit_url, HTTP_HX_REQUEST="true")
    assert edit_get.status_code == 200
    assert b'<div class="modal fade"' in edit_get.content
    assert b"modal-dialog" in edit_get.content
    assert b'<div class="modal-content"' in edit_get.content
    assert b'hx-target="#person-action-modal-container"' in edit_get.content
    assert b'type="submit"' in edit_get.content
    invalid = client.post(edit_url, _payload(first_name="", last_name="Entered"))
    assert invalid.status_code == 200 and b"Entered" in invalid.content
    person.refresh_from_db()
    assert person.first_name == "Ada"
    assert client.post(edit_url, _payload()).status_code == 302
    person.refresh_from_db()
    assert person.display_name == "Grace Hopper"
    assert AuditEvent.objects.filter(action="personnel.updated", target_uuid=person.id).exists()
    assert PublicationJob.objects.filter(department=department, station=station).exists()

    htmx_success = client.post(edit_url, _payload(), HTTP_HX_REQUEST="true")
    assert htmx_success.status_code == 204
    assert htmx_success["HX-Redirect"] == detail_url

    protected = client.post(reverse("personnel-delete", args=(department.id, person.id)))
    assert protected.status_code == 200
    assert Person.objects.filter(pk=person.id).exists()
    assert b'hx-target="#person-action-modal-container"' in protected.content
    assert not AuditEvent.objects.filter(action="personnel.deleted", target_uuid=person.id).exists()

    safe = Person.objects.create(
        department=department,
        personnel_number="ERR-1",
        first_name="Erroneous",
        last_name="Record",
        display_name="Erroneous Record",
    )
    delete_url = reverse("personnel-delete", args=(department.id, safe.id))
    assert client.get(delete_url, HTTP_HX_REQUEST="true").status_code == 200
    assert client.post(delete_url).status_code == 302
    assert not Person.objects.filter(pk=safe.id).exists()
    assert AuditEvent.objects.filter(action="personnel.deleted", target_uuid=safe.id).exists()


@pytest.mark.django_db
def test_personnel_cross_scope_get_safety_and_csrf(client, personnel_scope):
    admin, station_admin, department, station, _, person = personnel_scope
    client.force_login(station_admin)
    assert client.get(reverse("personnel-edit", args=(department.id, person.id))).status_code == 403
    client.force_login(admin)
    assert client.get(reverse("personnel-edit", args=(department.id, person.id))).status_code == 200
    person.refresh_from_db()
    assert person.display_name == "Ada Lovelace"
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(admin)
    assert (
        csrf_client.post(
            reverse("personnel-edit", args=(department.id, person.id)), _payload()
        ).status_code
        == 403
    )
    person.refresh_from_db()
    assert person.display_name == "Ada Lovelace"
