import time
from datetime import timedelta

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.personnel.services import (
    create_person,
    offboard_person,
    set_commander_eligibility,
    set_commander_email,
    set_retention_policy,
    verify_commander_email,
)
from apps.publications.builders import build_summary
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.publications.services import claim_next_job


@pytest.fixture
def department_admin(db):
    user = User.objects.create_user("admin@example.test", "Department Admin", "safe-password")
    department = Department.objects.create(
        name="Test Department", short_code="TEST", created_by=user
    )
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station One", short_code="ONE")
    return user, department, station


def _login_with_reauth(client, user) -> None:
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()


def _messages(response) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.mark.django_db(transaction=True)
def test_rejected_commander_email_has_no_side_effects(client, department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="42",
        first_name="Alex",
        last_name="Member",
    )
    assert person.incident_commander_eligible is False

    scope = DatasetScopeState.objects.get(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    job = PublicationJob.objects.get(department=department, station=station)
    original_updated_at = person.updated_at
    original_revision = scope.source_revision
    original_dirty_since = scope.dirty_since
    original_job_revision = job.source_revision
    audit_before = AuditEvent.objects.count()

    client.force_login(actor)
    response = client.post(
        reverse("personnel-email", args=(department.id, person.id)),
        {"email": "commander@example.test"},
    )

    assert response.status_code == 302
    assert any("eligibility" in message.lower() for message in _messages(response))

    person.refresh_from_db()
    scope.refresh_from_db()
    job.refresh_from_db()
    assert person.incident_commander_email is None
    assert person.email_verified_at is None
    assert person.email_verified_by is None
    assert person.updated_at == original_updated_at
    assert AuditEvent.objects.count() == audit_before
    assert scope.source_revision == original_revision
    assert scope.dirty_since == original_dirty_since
    active_jobs = PublicationJob.objects.filter(
        department=department,
        station=station,
        status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
    )
    assert active_jobs.count() == 1
    assert job.source_revision == original_job_revision
    assert job.status == PublicationJob.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_verify_email_rejected_for_ineligible_person_is_handled(client, department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="43",
        first_name="Taylor",
        last_name="Member",
    )
    _login_with_reauth(client, actor)
    response = client.post(reverse("personnel-verify-email", args=(department.id, person.id)))

    assert response.status_code == 302
    assert any("eligible" in message.lower() for message in _messages(response))


@pytest.mark.django_db(transaction=True)
def test_offboard_without_retention_policy_is_handled(client, department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="44",
        first_name="Jordan",
        last_name="Member",
    )
    _login_with_reauth(client, actor)
    response = client.post(reverse("personnel-offboard", args=(department.id, person.id)))

    assert response.status_code == 302
    assert any("retention policy" in message.lower() for message in _messages(response))
    person.refresh_from_db()
    assert person.lifecycle_status == Person.LifecycleStatus.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_anonymize_before_retention_expiry_is_handled(client, department_admin):
    actor, department, station = department_admin
    set_retention_policy(actor=actor, department=department, retention_period=timedelta(days=30))
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="45",
        first_name="Morgan",
        last_name="Member",
    )
    offboard_person(actor=actor, person=person)
    _login_with_reauth(client, actor)
    response = client.post(reverse("personnel-anonymize", args=(department.id, person.id)))

    assert response.status_code == 302
    assert any("retention" in message.lower() for message in _messages(response))
    person.refresh_from_db()
    assert person.lifecycle_status == Person.LifecycleStatus.DEPARTED


@pytest.mark.django_db(transaction=True)
def test_commander_eligibility_for_departed_person_is_handled(client, department_admin):
    actor, department, station = department_admin
    set_retention_policy(actor=actor, department=department, retention_period=timedelta(days=30))
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="46",
        first_name="Casey",
        last_name="Member",
    )
    offboard_person(actor=actor, person=person)
    client.force_login(actor)
    response = client.post(
        reverse("personnel-eligibility", args=(department.id, person.id)), {"eligible": "on"}
    )

    assert response.status_code == 302
    assert any("active" in message.lower() for message in _messages(response))


@pytest.mark.django_db(transaction=True)
def test_valid_second_change_while_dirty_coalesces_and_builds_latest(department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="47",
        first_name="Riley",
        last_name="Member",
    )
    scope = DatasetScopeState.objects.get(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    assert scope.source_revision == 1

    # Change A: make the person commander-eligible.
    set_commander_eligibility(actor=actor, person=person, eligible=True)
    scope.refresh_from_db()
    assert scope.source_revision == 2

    # Change B: a second valid change on the same scope (now ineligible again),
    # before any build runs.
    set_commander_eligibility(actor=actor, person=person, eligible=False)
    scope.refresh_from_db()
    assert scope.source_revision == 3

    jobs = PublicationJob.objects.filter(department=department, station=station)
    assert jobs.count() == 1
    job = jobs.get()
    assert job.status == PublicationJob.Status.PENDING
    assert job.source_revision == 3

    PublicationJob.objects.filter(pk=job.pk).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    claimed = claim_next_job()
    assert claimed is not None
    assert claimed.source_revision == 3
    assert claimed.build_publication is not None
    assert claimed.build_publication.source_revision == 3
    assert claimed.build_publication.status == DatasetPublication.Status.BUILDING
    assert (
        PublicationJob.objects.filter(
            department=department,
            station=station,
            status__in=(PublicationJob.Status.PENDING, PublicationJob.Status.RUNNING),
        ).count()
        == 1
    )

    # The build is anchored to the latest scope state: the summary reflects change
    # B (ineligible) rather than the stale state A (eligible).
    definition = get_dataset_definition("station_personnel")
    summary = build_summary(
        definition=definition,
        department=department,
        station=station,
        source_revision=claimed.source_revision,
    )
    assert summary["source_revision"] == 3
    assert summary["commander_eligible_count"] == 0


@pytest.mark.django_db(transaction=True)
def test_eligibility_toggle_retains_email_but_clears_verification(department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="48",
        first_name="Quinn",
        last_name="Member",
    )
    set_commander_eligibility(actor=actor, person=person, eligible=True)
    set_commander_email(actor=actor, person=person, email="cmd@example.test")
    verify_commander_email(actor=actor, person=person)
    person.refresh_from_db()
    assert person.email_verified_at is not None

    set_commander_eligibility(actor=actor, person=person, eligible=False)
    person.refresh_from_db()
    # The email value is retained; only the verification claim is cleared.
    assert person.incident_commander_email == "cmd@example.test"
    assert person.email_verified_at is None
    assert person.email_verified_by is None

    set_commander_eligibility(actor=actor, person=person, eligible=True)
    person.refresh_from_db()
    assert person.incident_commander_email == "cmd@example.test"
    assert person.email_verified_at is None


@pytest.mark.django_db(transaction=True)
def test_permission_denied_is_not_converted_to_302(client, department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="49",
        first_name="Sam",
        last_name="Member",
    )
    station_admin = User.objects.create_user(
        "station@example.test", "Station Admin", "safe-password"
    )
    StationAdminAssignment.objects.create(
        user=station_admin, station=station, active=True, created_by=actor
    )
    client.force_login(station_admin)
    response = client.post(
        reverse("personnel-email", args=(department.id, person.id)),
        {"email": "cmd@example.test"},
    )
    assert response.status_code == 403
    assert response.status_code != 302


@pytest.mark.django_db(transaction=True)
def test_offboard_success_message_only_on_success(client, department_admin):
    actor, department, station = department_admin
    person = create_person(
        actor=actor,
        department=department,
        home_station=station,
        personnel_number="50",
        first_name="Riley",
        last_name="Member",
    )
    _login_with_reauth(client, actor)

    failed = client.post(reverse("personnel-offboard", args=(department.id, person.id)))
    failed_messages = _messages(failed)
    assert any("retention policy" in message.lower() for message in failed_messages)
    assert not any("offboarded" in message.lower() for message in failed_messages)

    set_retention_policy(actor=actor, department=department, retention_period=timedelta(days=30))
    succeeded = client.post(reverse("personnel-offboard", args=(department.id, person.id)))
    succeeded_messages = _messages(succeeded)
    assert any("offboarded" in message.lower() for message in succeeded_messages)
