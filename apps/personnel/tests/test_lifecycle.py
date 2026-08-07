from datetime import timedelta

import pytest
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.personnel.services import (
    PersonnelError,
    anonymize_person,
    create_person,
    offboard_person,
    set_retention_policy,
)


@pytest.fixture
def department_admin(db):
    from apps.accounts.models import User

    user = User.objects.create_user("admin@example.test", "Department Admin", "safe-password")
    department = Department.objects.create(
        name="Test Department", short_code="TEST", created_by=user
    )
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station One", short_code="ONE")
    return user, department, station


@pytest.mark.django_db
def test_offboarding_requires_policy_and_closes_current_home(department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="42",
        first_name="Alex",
        last_name="Member",
    )

    with pytest.raises(PersonnelError, match="retention policy"):
        offboard_person(actor=actor, person=person)

    set_retention_policy(actor=actor, department=department, retention_period=timedelta(days=30))
    departed = offboard_person(actor=actor, person=person)

    assignment = PersonnelStationAssignment.objects.get(person=person)
    assert departed.lifecycle_status == Person.LifecycleStatus.DEPARTED
    assert departed.active is False
    assert departed.retention_until is not None
    assert assignment.ended_at is not None
    assert assignment.valid_until is not None


@pytest.mark.django_db
def test_anonymization_removes_identifying_fields_after_retention(department_admin):
    actor, department, station = department_admin
    set_retention_policy(actor=actor, department=department, retention_period=timedelta(days=1))
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="43",
        first_name="Taylor",
        last_name="Member",
    )
    offboard_person(actor=actor, person=person)
    person.retention_until = timezone.now() - timedelta(seconds=1)
    person.save(update_fields=("retention_until",))

    anonymized = anonymize_person(actor=actor, person=person)

    assert anonymized.lifecycle_status == Person.LifecycleStatus.ANONYMIZED
    assert anonymized.display_name == "Former member"
    assert anonymized.first_name is None
    assert anonymized.last_name is None
    assert anonymized.personnel_number is None
