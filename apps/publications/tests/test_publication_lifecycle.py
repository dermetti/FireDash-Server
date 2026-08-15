"""Lifecycle regression tests for automatically-dirtied publication datasets."""

import hashlib
import io
import json
import uuid
import zipfile
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.services import create_temporary_assignment, end_temporary_assignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.personnel.services import create_person, offboard_person, set_retention_policy
from apps.publications.builders import build_artifact
from apps.publications.models import PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan, Hydrant
from apps.reference_data.services import create_hydrant, set_fire_plan_active, update_hydrant


@pytest.fixture
def lifecycle_context(db):
    admin = User.objects.create_user("lifecycle@example.test", "Lifecycle Admin", "safe-password")
    department = Department.objects.create(
        name="Lifecycle Dept", short_code="LFC", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    receiving = Station.objects.create(department=department, name="Station B", short_code="STB")
    return admin, department, station, receiving


@pytest.mark.django_db(transaction=True)
def test_hydrant_mutations_coalesce_into_one_debounced_job(lifecycle_context):
    admin, department, _, _ = lifecycle_context
    hydrant = create_hydrant(actor=admin, department=department, longitude=10.0, latitude=53.0)
    update_hydrant(actor=admin, hydrant=hydrant, diameter_mm=150)

    jobs = PublicationJob.objects.filter(
        department=department, dataset_type_code="department_hydrants"
    )
    assert jobs.count() == 1
    job = jobs.get()
    assert job.status == PublicationJob.Status.PENDING
    assert job.trigger_type == PublicationJob.TriggerType.DATA_CHANGE
    assert job.source_revision == 2
    assert job.not_before is not None


@pytest.mark.django_db(transaction=True)
def test_personnel_temporary_assignment_included_in_receiving_station(lifecycle_context):
    admin, department, station, receiving = lifecycle_context
    person = create_person(
        actor=admin,
        department=department,
        home_station=station,
        personnel_number="P-1",
        first_name="Ada",
        last_name="Lovelace",
    )
    create_temporary_assignment(
        person=person,
        station=receiving,
        actor=admin,
        valid_until=timezone.now() + timedelta(hours=1),
    )

    definition = get_dataset_definition("station_personnel")
    artifact = build_artifact(
        definition=definition, department=department, station=receiving, source_revision=1
    )
    document = json.loads(artifact.decode("utf-8"))

    assert document["station_id"] == str(receiving.id)
    assert [p["id"] for p in document["people"]] == [str(person.id)]


@pytest.mark.django_db(transaction=True)
def test_offboarded_person_removed_from_publication(lifecycle_context):
    admin, department, station, _ = lifecycle_context
    set_retention_policy(actor=admin, department=department, retention_period=timedelta(days=30))
    person = create_person(
        actor=admin,
        department=department,
        home_station=station,
        personnel_number="P-2",
        first_name="Grace",
        last_name="Hopper",
    )
    offboard_person(actor=admin, person=person)

    definition = get_dataset_definition("station_personnel")
    artifact = build_artifact(
        definition=definition, department=department, station=station, source_revision=2
    )
    document = json.loads(artifact.decode("utf-8"))

    assert document["people"] == []


@pytest.mark.django_db(transaction=True)
def test_fire_plan_deactivation_marks_dirty_and_queues(lifecycle_context):
    admin, department, _, _ = lifecycle_context
    plan = FirePlan.objects.create(
        department=department,
        object_name="Site A",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="site-a.pdf",
        file_size=100,
        page_count=1,
        sha256="a" * 64,
        uploaded_by=admin,
    )
    set_fire_plan_active(actor=admin, fire_plan=plan, active=False)

    jobs = PublicationJob.objects.filter(
        department=department, dataset_type_code="department_fire_plans"
    )
    assert jobs.count() == 1
    assert jobs.get().status == PublicationJob.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_inactive_hydrant_excluded_from_artifact(lifecycle_context):
    admin, department, _, _ = lifecycle_context
    active = create_hydrant(actor=admin, department=department, longitude=10.0, latitude=53.0)
    inactive = create_hydrant(actor=admin, department=department, longitude=10.1, latitude=53.1)
    inactive.status = Hydrant.Status.INACTIVE
    inactive.save(update_fields=("status", "updated_at"))

    definition = get_dataset_definition("department_hydrants")
    artifact = build_artifact(
        definition=definition, department=department, station=None, source_revision=1
    )
    document = json.loads(artifact.decode("utf-8"))

    assert [feature["id"] for feature in document["features"]] == [str(active.id)]
    assert str(inactive.id) not in [feature["id"] for feature in document["features"]]


@pytest.mark.django_db(transaction=True)
def test_ended_temporary_assignment_removed_from_receiving_station(lifecycle_context):
    admin, department, station, receiving = lifecycle_context
    person = create_person(
        actor=admin,
        department=department,
        home_station=station,
        personnel_number="P-3",
        first_name="Ada",
        last_name="Byron",
    )
    assignment = create_temporary_assignment(
        person=person,
        station=receiving,
        actor=admin,
        valid_until=timezone.now() + timedelta(hours=1),
    )

    definition = get_dataset_definition("station_personnel")
    active = build_artifact(
        definition=definition, department=department, station=receiving, source_revision=1
    )
    assert [p["id"] for p in json.loads(active.decode("utf-8"))["people"]] == [str(person.id)]

    end_temporary_assignment(assignment=assignment, actor=admin)

    rebuilt = build_artifact(
        definition=definition, department=department, station=receiving, source_revision=2
    )
    assert json.loads(rebuilt.decode("utf-8"))["people"] == []


@pytest.mark.django_db(transaction=True)
def test_inactive_fire_plan_excluded_from_rebuilt_artifact(lifecycle_context, tmp_path):
    admin, department, _, _ = lifecycle_context
    pdf = b"%PDF-1.4\n1 0 obj\nendobj\n%%EOF\n"
    digest = hashlib.sha256(pdf).hexdigest()
    plan = FirePlan.objects.create(
        department=department,
        object_name="Site A",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="site-a.pdf",
        file_size=len(pdf),
        page_count=1,
        sha256=digest,
        uploaded_by=admin,
    )
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir(parents=True)
    (accepted_root / plan.document_key).write_bytes(pdf)

    definition = get_dataset_definition("department_fire_plans")
    with override_settings(REFERENCE_DATA_ACCEPTED_ROOT=accepted_root):
        active_artifact = build_artifact(
            definition=definition, department=department, station=None, source_revision=1
        )
    with zipfile.ZipFile(io.BytesIO(active_artifact)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert [entry["id"] for entry in manifest["fire_plans"]] == [str(plan.id)]

    set_fire_plan_active(actor=admin, fire_plan=plan, active=False)

    with override_settings(REFERENCE_DATA_ACCEPTED_ROOT=accepted_root):
        rebuilt = build_artifact(
            definition=definition, department=department, station=None, source_revision=2
        )
    with zipfile.ZipFile(io.BytesIO(rebuilt)) as archive:
        rebuilt_manifest = json.loads(archive.read("manifest.json"))
    assert rebuilt_manifest["fire_plans"] == []
