"""Data Hub publication-state presentation regressions."""

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import (
    DepartmentMembership,
    StationAdminAssignment,
    SystemRole,
)
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path
from apps.reference_data.models import PhonebookEntry


@pytest.fixture
def data_hub_scope(client, db):
    admin = User.objects.create_user("data-hub@example.test", "Data Hub", "safe-password")
    department = Department.objects.create(name="Data Hub", short_code="HUB", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    client.force_login(admin)
    return admin, department


def _scope(department):
    return DatasetScopeState.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        source_revision=17,
    )


def _publication(*, department, scope, version, status):
    publication_id = uuid.uuid4()
    values = {
        "id": publication_id,
        "department": department,
        "scope_type": scope.scope_type,
        "dataset_type_code": scope.dataset_type_code,
        "scope_state": scope,
        "version_number": version,
        "schema_version": 1,
        "source_revision": version,
        "status": status,
    }
    if status == DatasetPublication.Status.PUBLISHED:
        values.update(
            {
                "artifact_ready": True,
                "artifact_status": DatasetPublication.ArtifactStatus.READY,
                "artifact_path": publication_artifact_relative_path(
                    department_id=department.id if department else None,
                    publication_id=publication_id,
                    scope_type=scope.scope_type,
                ),
                "artifact_size": 1,
                "artifact_sha256": "a" * 64,
                "artifact_nonce": b"n" * 12,
                "artifact_wrapped_cek": b"w" * 40,
                "artifact_encryption_algorithm": "AES-256-GCM",
                "artifact_wrapping_algorithm": "AES-KW-RFC3394",
                "artifact_kek_version": "test",
                "artifact_signature": b"s" * 64,
                "artifact_signature_algorithm": "Ed25519",
                "artifact_signing_key_version": "test",
            }
        )
    return DatasetPublication.objects.create(**values)


def _activate(*, department, scope, version=17):
    publication = _publication(
        department=department,
        scope=scope,
        version=version,
        status=DatasetPublication.Status.PUBLISHED,
    )
    scope.latest_built_publication = publication
    scope.current_published_publication = publication
    publication.source_fingerprint = "a" * 64
    publication.save(update_fields=("source_fingerprint",))
    scope.current_source_fingerprint = publication.source_fingerprint
    scope.save(
        update_fields=(
            "latest_built_publication",
            "current_published_publication",
            "current_source_fingerprint",
        )
    )
    return publication


def _page(client, department):
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    assert response.status_code == 200
    return next(m for m in response.context["modules"] if m["id"] == "department_hydrants")


@pytest.mark.django_db
def test_data_hub_shows_authoritative_current_publication(data_hub_scope, client):
    _admin, department = data_hub_scope
    _activate(department=department, scope=_scope(department))

    content = _page(client, department)
    assert content["publication_version"] == 17
    assert content["publication_state"] == "Current"


@pytest.mark.django_db
def test_data_hub_shows_scheduled_update_without_promoting_candidate(data_hub_scope, client):
    admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.BUILDING,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=18,
        status=PublicationJob.Status.PENDING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        requested_by=admin,
        not_before=timezone.now() + timedelta(minutes=1),
        build_publication=candidate,
    )

    content = _page(client, department)
    assert content["publication_version"] == 17
    assert content["publication_state"] == "Update scheduled"


@pytest.mark.django_db
def test_data_hub_shows_building_update_without_promoting_candidate(data_hub_scope, client):
    admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.BUILDING,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=18,
        status=PublicationJob.Status.RUNNING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        requested_by=admin,
        build_publication=candidate,
    )

    content = _page(client, department)
    assert content["publication_version"] == 17
    assert content["publication_state"] == "Building update"


@pytest.mark.django_db
def test_data_hub_shows_failed_update_without_replacing_current(data_hub_scope, client):
    _admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    failed = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.FAILED,
    )
    scope.latest_built_publication = failed
    # A failed update is only current card state while canonical source still
    # differs from the active publication.
    scope.current_source_fingerprint = "b" * 64
    scope.save(update_fields=("latest_built_publication", "current_source_fingerprint"))

    content = _page(client, department)
    assert content["publication_version"] == 17
    assert content["publication_state"] == "Update failed"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        DatasetPublication.Status.FAILED,
        DatasetPublication.Status.BUILDING,
        DatasetPublication.Status.PUBLISHED,
    ],
)
def test_data_hub_shows_not_published_without_an_active_publication(data_hub_scope, client, status):
    _admin, department = data_hub_scope
    scope = _scope(department)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=status,
    )
    scope.latest_built_publication = candidate
    scope.save(update_fields=("latest_built_publication",))

    content = _page(client, department)
    assert content["publication_version"] is None
    assert content["publication_state"] is None


@pytest.mark.django_db
def test_data_hub_publication_state_is_department_scoped(data_hub_scope, client):
    _admin, department = data_hub_scope
    _activate(department=department, scope=_scope(department), version=17)
    outsider = User.objects.create_user("other-hub@example.test", "Other Hub", "safe-password")
    other_department = Department.objects.create(
        name="Other Data Hub", short_code="OHD", created_by=outsider
    )
    other_scope = _scope(other_department)
    _publication(
        department=other_department,
        scope=other_scope,
        version=99,
        status=DatasetPublication.Status.FAILED,
    )

    content = _page(client, department)
    assert content["publication_version"] == 17
    assert content["publication_state"] == "Current"


@pytest.mark.django_db
def test_data_hub_counts_and_access_remain_department_scoped(data_hub_scope, client):
    admin, department = data_hub_scope
    outsider = User.objects.create_user("outside@example.test", "Outside", "safe-password")
    other = Department.objects.create(name="Other", short_code="OTH", created_by=outsider)
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=outsider)
    PhonebookEntry.objects.create(
        department=department, organization_unit="Local", phone_number="1"
    )
    for index in range(2):
        PhonebookEntry.objects.create(
            department=other, organization_unit="Other", phone_number=str(index)
        )
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    phonebook = next(m for m in response.context["modules"] if m["id"] == "department_phonebook")
    assert phonebook["count"] == 1
    assert client.get(reverse("portal-data-hub", args=(other.id,))).status_code == 403
    assert client.get(reverse("portal-system-data-hub")).status_code == 403
    station = Station.objects.create(department=department, name="Station", short_code="ST")
    StationAdminAssignment.objects.create(user=outsider, station=station, created_by=admin)
    client.force_login(outsider)
    assert client.get(reverse("portal-data-hub", args=(department.id,))).status_code == 403
    assert client.get(reverse("portal-system-data-hub")).status_code == 403
    client.logout()
    assert client.get(reverse("portal-data-hub", args=(department.id,))).status_code == 302
    assert client.get(reverse("portal-system-data-hub")).status_code == 302


@pytest.mark.django_db
@pytest.mark.parametrize("published", [False, True])
def test_system_goods_state_and_read_only_department_card(data_hub_scope, client, published):
    admin, department = data_hub_scope
    SystemRole.objects.create(user=admin)
    scope, _ = DatasetScopeState.objects.update_or_create(
        scope_type="SYSTEM",
        department=None,
        station=None,
        dataset_type_code="dangerous_goods",
        defaults={"source_revision": 23},
    )
    if published:
        _activate(department=None, scope=scope, version=17)
    candidate = _publication(
        department=None, scope=scope, version=18, status=DatasetPublication.Status.BUILDING
    )
    PublicationJob.objects.create(
        scope_type="SYSTEM",
        department=None,
        dataset_type_code="dangerous_goods",
        scope_state=scope,
        source_revision=23,
        status=PublicationJob.Status.PENDING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        requested_by=admin,
        not_before=timezone.now() + timedelta(minutes=1),
        build_publication=candidate,
    )
    response = client.get(reverse("portal-system-data-hub"))
    goods = next(m for m in response.context["modules"] if m["id"] == "dangerous_goods")
    assert goods["publication_version"] == (17 if published else None)
    assert goods["publication_state"] == ("Update scheduled" if published else None)
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    inherited = next(
        m
        for m in response.context["inherited_system_configurations"]
        if m["id"] == "dangerous_goods"
    )
    assert inherited["publication_version"] == goods["publication_version"]
    assert inherited["publication_state"] == goods["publication_state"]


@pytest.mark.django_db
def test_system_goods_current_publication_has_no_pending_update(data_hub_scope, client):
    admin, department = data_hub_scope
    SystemRole.objects.create(user=admin)
    scope = DatasetScopeState.objects.get(scope_type="SYSTEM", dataset_type_code="dangerous_goods")
    _activate(department=None, scope=scope, version=7)
    response = client.get(reverse("portal-system-data-hub"))
    goods = next(m for m in response.context["modules"] if m["id"] == "dangerous_goods")
    assert goods["publication_version"] == 7
    assert goods["publication_state"] == "Current"
