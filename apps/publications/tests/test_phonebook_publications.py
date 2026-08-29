import json

import pytest
from django.db import DatabaseError

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.builders import build_artifact, build_source_payload, source_fingerprint
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.publications.services import mark_dirty
from apps.reference_data.services import (
    create_phonebook_entry,
    delete_phonebook_entry,
    update_phonebook_entry,
)


@pytest.fixture
def phonebook_scope(db):
    actor = User.objects.create_user("publication-phonebook@example.test", "Publisher", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    first = Station.objects.create(department=department, name="First", short_code="F1")
    second = Station.objects.create(department=department, name="Second", short_code="F2")
    return actor, department, first, second


@pytest.mark.django_db
def test_phonebook_projection_is_pure_scoped_and_deterministic(phonebook_scope):
    actor, department, first, second = phonebook_scope
    department_entry = create_phonebook_entry(
        actor=actor,
        department=department,
        organization_unit="Control",
        phone_number="040 428512300",
    )
    first_entry = create_phonebook_entry(
        actor=actor,
        department=department,
        station=first,
        organization_unit="First",
        phone_number="040 428512301",
    )
    create_phonebook_entry(
        actor=actor,
        department=department,
        station=second,
        organization_unit="Second",
        phone_number="040 428512302",
    )
    department_definition = get_dataset_definition("department_phonebook")
    station_definition = get_dataset_definition("station_phonebook")
    department_payload = build_source_payload(
        definition=department_definition, department=department, station=None
    )
    station_payload = build_source_payload(
        definition=station_definition, department=department, station=first
    )
    assert [entry["id"] for entry in department_payload["entries"]] == [str(department_entry.id)]
    assert [entry["id"] for entry in station_payload["entries"]] == [str(first_entry.id)]
    artifact = build_artifact(
        definition=station_definition, department=department, station=first, source_revision=1
    )
    assert json.loads(artifact)["entries"] == station_payload["entries"]
    assert source_fingerprint(
        definition=department_definition, department=department, station=None
    ) == source_fingerprint(definition=department_definition, department=department, station=None)


@pytest.mark.django_db
def test_phonebook_mutations_dirty_only_old_and_new_pure_scopes(phonebook_scope):
    actor, department, first, second = phonebook_scope
    entry = create_phonebook_entry(
        actor=actor,
        department=department,
        station=first,
        organization_unit="First",
        phone_number="1",
    )
    assert DatasetScopeState.objects.filter(
        department=department, station=first, dataset_type_code="station_phonebook"
    ).exists()
    assert not DatasetScopeState.objects.filter(
        department=department, station__isnull=True, dataset_type_code="department_phonebook"
    ).exists()
    update_phonebook_entry(actor=actor, entry=entry, station=second)
    assert DatasetScopeState.objects.filter(
        department=department, station=second, dataset_type_code="station_phonebook"
    ).exists()
    update_phonebook_entry(actor=actor, entry=entry, station=None)
    assert DatasetScopeState.objects.filter(
        department=department, station=None, dataset_type_code="department_phonebook"
    ).exists()
    delete_phonebook_entry(actor=actor, entry=entry)
    assert PublicationJob.objects.filter(
        dataset_type_code__in=("department_phonebook", "station_phonebook"), department=department
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_postgres_scope_guard_accepts_phonebook_and_rejects_invalid_scope(phonebook_scope):
    actor, department, first, _ = phonebook_scope
    department_scope = mark_dirty(
        department=department, dataset_type_code="department_phonebook", actor=actor
    )
    station_scope = mark_dirty(
        department=department, station=first, dataset_type_code="station_phonebook", actor=actor
    )
    assert department_scope.station_id is None
    assert station_scope.station_id == first.id
    with pytest.raises(DatabaseError, match="Dataset type station scope is invalid"):
        DatasetScopeState.objects.create(
            department=department, station=first, dataset_type_code="department_phonebook"
        )
    with pytest.raises(DatabaseError, match="Dataset type station scope is invalid"):
        DatasetScopeState.objects.create(
            department=department, dataset_type_code="station_phonebook"
        )
    foreign_department = Department.objects.create(
        name="Foreign", short_code="FOR", created_by=actor
    )
    foreign_station = Station.objects.create(
        department=foreign_department, name="Foreign station", short_code="FS"
    )
    with pytest.raises(DatabaseError, match="Station must belong to the scope department"):
        DatasetScopeState.objects.create(
            department=department, station=foreign_station, dataset_type_code="station_phonebook"
        )
    with pytest.raises(DatabaseError, match="Unknown dataset type code"):
        DatasetScopeState.objects.create(department=department, dataset_type_code="unknown")
    assert (
        mark_dirty(
            department=department, dataset_type_code="department_hydrants", actor=actor
        ).dataset_type_code
        == "department_hydrants"
    )
