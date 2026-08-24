"""Personnel CSV Home Station contract and staged review integration."""

from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse

from apps.accounts.models import User
from apps.assignments.models import PersonnelStationAssignment
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import ImportError, apply_preview, create_preview
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.publications.models import DatasetScopeState

HEADER = "personnel_number,first_name,last_name,home_station,incident_commander_eligible\n"


def personnel_csv(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


@pytest.fixture
def personnel_import_context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "personnel-import-staging"
    actor = User.objects.create_user(
        "personnel-import@example.test", "Import Admin", "safe-password"
    )
    department = Department.objects.create(name="Personnel", short_code="PER", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    return actor, department, other


def preview(actor, department, payload):
    return create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.UPSERT,
        filename="personnel.csv",
        payload=payload,
    )


@pytest.mark.django_db(transaction=True)
def test_home_station_matches_short_code_and_full_name(personnel_import_context):
    actor, department, _ = personnel_import_context
    station = Station.objects.create(department=department, short_code="F25", name="Station 25")
    batch = preview(
        actor,
        department,
        personnel_csv("P-1,Ada,Lovelace, F25 ,true", "P-2,Grace,Hopper, station 25 ,false"),
    )
    assert batch.validation_summary["review_items"] == []
    assert batch.normalized_intent["rows"][0]["home_station_resolution"]["station_id"] == str(
        station.id
    )
    apply_preview(actor=actor, batch_id=batch.id)
    assert set(
        Person.objects.filter(department=department).values_list("personnel_number", flat=True)
    ) == {
        "P-1",
        "P-2",
    }
    assert (
        PersonnelStationAssignment.objects.filter(station=station, assignment_type="HOME").count()
        == 2
    )


@pytest.mark.django_db(transaction=True)
def test_missing_and_foreign_home_station_stay_staged_and_never_create_station(
    personnel_import_context,
):
    actor, department, other = personnel_import_context
    Station.objects.create(department=other, short_code="F99", name="Foreign")
    batch = preview(actor, department, personnel_csv("P-1,Ada,Lovelace,F99,false"))
    item = batch.validation_summary["review_items"][0]
    assert item["kind"] == "personnel_missing_home_station"
    assert not Station.objects.filter(department=department).exists()
    with pytest.raises(ImportError, match="Resolve each Home Station"):
        apply_preview(actor=actor, batch_id=batch.id)
    assert not Person.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_missing_home_station_skip_uses_shared_review_region_and_excludes_row(
    client, personnel_import_context
):
    actor, department, _ = personnel_import_context
    batch = preview(actor, department, personnel_csv("P-1,Ada,Lovelace,Missing,false"))
    item = batch.validation_summary["review_items"][0]
    client.force_login(actor)
    response = client.post(
        reverse("ingestion-review-skip", args=(department.id, batch.id, item["key"])),
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert '<div id="import-review-region">' in response.content.decode()
    batch.refresh_from_db()
    assert batch.validation_summary["review_decisions"][item["key"]]["decision"] == "skipped"
    apply_preview(actor=actor, batch_id=batch.id)
    assert not Person.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_ambiguous_home_station_requires_same_department_review_choice(
    client, personnel_import_context
):
    actor, department, other = personnel_import_context
    first = Station.objects.create(department=department, short_code="F1", name="Mitte")
    Station.objects.create(department=department, short_code="F2", name="Mitte")
    foreign = Station.objects.create(department=other, short_code="F3", name="Mitte")
    batch = preview(actor, department, personnel_csv("P-1,Ada,Lovelace,Mitte,false"))
    item = batch.validation_summary["review_items"][0]
    assert item["kind"] == "personnel_ambiguous_home_station"

    client.force_login(actor)
    response = client.post(
        reverse(
            "ingestion-review-personnel-home-station", args=(department.id, batch.id, item["key"])
        ),
        {"station_id": str(foreign.id)},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert "Select a valid choice" in response.content.decode()
    batch.refresh_from_db()
    assert item["key"] not in batch.validation_summary["review_decisions"]

    response = client.post(
        reverse(
            "ingestion-review-personnel-home-station", args=(department.id, batch.id, item["key"])
        ),
        {"station_id": str(first.id)},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.validation_summary["review_decisions"][item["key"]]["decision"] == "approved"
    assert not Person.objects.filter(department=department).exists()
    apply_preview(actor=actor, batch_id=batch.id)
    assert (
        PersonnelStationAssignment.objects.get(person__personnel_number="P-1").station_id
        == first.id
    )


@pytest.mark.django_db(transaction=True)
def test_existing_person_blank_home_station_retains_assignment_and_noop_does_not_dirty(
    personnel_import_context,
):
    actor, department, _ = personnel_import_context
    station = Station.objects.create(department=department, short_code="F25", name="Station 25")
    person = Person.objects.create(
        department=department,
        personnel_number="P-1",
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
    )
    PersonnelStationAssignment.objects.create(
        person=person,
        station=station,
        assignment_type="HOME",
        valid_from=person.created_at,
        created_by=actor,
    )
    batch = preview(actor, department, personnel_csv("P-1,Ada,Lovelace,,false"))
    apply_preview(actor=actor, batch_id=batch.id)
    assert batch.unchanged_count == 1
    assert not DatasetScopeState.objects.filter(department=department).exists()
    assert (
        PersonnelStationAssignment.objects.get(person=person, ended_at__isnull=True).station_id
        == station.id
    )


@pytest.mark.django_db(transaction=True)
def test_new_and_changed_personnel_dirty_only_affected_home_scopes(personnel_import_context):
    actor, department, _ = personnel_import_context
    old_station = Station.objects.create(department=department, short_code="F1", name="Old")
    new_station = Station.objects.create(department=department, short_code="F2", name="New")
    person = Person.objects.create(
        department=department,
        personnel_number="P-1",
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
    )
    PersonnelStationAssignment.objects.create(
        person=person,
        station=old_station,
        assignment_type="HOME",
        valid_from=person.created_at,
        created_by=actor,
    )
    batch = preview(
        actor,
        department,
        personnel_csv("P-1,Ada,Byron,F2,true", "P-2,Grace,Hopper,F2,false"),
    )
    apply_preview(actor=actor, batch_id=batch.id)
    assert DatasetScopeState.objects.filter(
        department=department, dataset_type_code="station_personnel", station=old_station
    ).exists()
    assert DatasetScopeState.objects.filter(
        department=department, dataset_type_code="station_personnel", station=new_station
    ).exists()
    person.refresh_from_db()
    assert person.display_name == "Ada Byron"
    assert person.incident_commander_eligible is True


@pytest.mark.django_db
def test_personnel_import_page_template_uses_home_station_contract(
    client, personnel_import_context
):
    actor, department, _ = personnel_import_context
    client.force_login(actor)
    response = client.get(reverse("ingestion-import-personnel", args=(department.id,)))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Import Personnel" in content
    assert "Import and review" in content
    assert "home_station" not in content  # CSV header belongs in the downloadable template.
    assert 'name="import_format"' in content and 'type="hidden"' in content
    template_path = finders.find("ingestion/templates/personnel-v1.csv")
    assert template_path is not None
    header = Path(template_path).read_text(encoding="utf-8").splitlines()[0]
    assert (
        header == "personnel_number,first_name,last_name,home_station,incident_commander_eligible"
    )
    assert "import_mode" not in header
