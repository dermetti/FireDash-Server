"""Phase 2C Personnel create modal and lifecycle explanation regressions."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.personnel.models import Person


@pytest.fixture
def personnel_ui_context(db):
    actor = User.objects.create_user(
        "personnel-ui@example.test", "Personnel Admin", "safe-password"
    )
    department = Department.objects.create(name="Personnel", short_code="PER", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    station = Station.objects.create(department=department, name="Station", short_code="F25")
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    foreign_station = Station.objects.create(department=other, name="Foreign", short_code="F99")
    return actor, department, station, foreign_station


@pytest.mark.django_db
def test_create_person_modal_has_bootstrap_fields_and_bound_validation(
    client, personnel_ui_context
):
    actor, department, station, foreign_station = personnel_ui_context
    client.force_login(actor)
    url = reverse("personnel-create", args=(department.id,))
    response = client.get(url, HTTP_HX_REQUEST="true")
    content = response.content.decode()
    assert response.status_code == 200
    assert 'class="modal-dialog modal-dialog-scrollable modal-fullscreen-sm-down"' in content
    assert "Home Station" in content
    assert "Commander eligible" in content
    assert "Commander email" in content
    assert 'class="form-control"' in content and 'class="form-select"' in content

    response = client.post(
        url,
        {
            "personnel_number": "P-1",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "home_station": str(foreign_station.id),
            "incident_commander_email": "not-an-email",
        },
        HTTP_HX_REQUEST="true",
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'value="P-1"' in content
    assert "Select a valid choice" in content
    assert "Enter a valid email address" in content
    assert not Person.objects.filter(department=department).exists()

    response = client.post(
        url,
        {
            "personnel_number": "P-1",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "home_station": str(station.id),
            "incident_commander_eligible": "on",
            "incident_commander_email": "commander@example.test",
        },
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204
    person = Person.objects.get(department=department, personnel_number="P-1")
    assert person.incident_commander_eligible is True
    assert person.incident_commander_email == "commander@example.test"


@pytest.mark.django_db
def test_person_detail_explains_distinct_offboard_and_anonymize_consequences(
    client, personnel_ui_context
):
    actor, department, station, _ = personnel_ui_context
    person = Person.objects.create(
        department=department,
        personnel_number="P-1",
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
    )
    client.force_login(actor)
    response = client.get(reverse("personnel-detail", args=(department.id, person.id)))
    content = response.content.decode()
    assert "Offboard ends active operational use" in content
    assert "Anonymize is a later privacy action" in content
