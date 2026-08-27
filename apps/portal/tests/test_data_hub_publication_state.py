"""Data Hub publication-state presentation regressions."""

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path


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
                    department_id=department.id, publication_id=publication_id
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
    return client.get(reverse("portal-data-hub", args=(department.id,))).content.decode()


@pytest.mark.django_db
def test_data_hub_shows_authoritative_current_publication(data_hub_scope, client):
    _admin, department = data_hub_scope
    _activate(department=department, scope=_scope(department))

    content = _page(client, department)

    assert "v17 &middot; Current" in content


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

    assert "v17 &middot; Update scheduled" in content
    assert "v18 &middot;" not in content


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

    assert "v17 &middot; Building update" in content
    assert "v18 &middot;" not in content


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

    assert "v17 &middot; Update failed" in content
    assert "v18 &middot;" not in content


@pytest.mark.django_db
def test_data_hub_shows_not_published_without_an_active_publication(data_hub_scope, client):
    _admin, department = data_hub_scope
    scope = _scope(department)
    _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.FAILED,
    )

    content = _page(client, department)

    assert "Not published" in content
    assert "v18 &middot;" not in content


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

    assert "v17 &middot; Current" in content
    assert "v99 &middot;" not in content
    assert "Update failed" not in content


@pytest.mark.django_db
def test_data_hub_cards_remain_single_navigation_links(data_hub_scope, client):
    _admin, department = data_hub_scope

    content = _page(client, department)

    assert '<a class="card h-100 d-block text-decoration-none text-reset shadow-sm"' in content
    assert "Open module" not in content
    assert "Import and review" not in content
    card_start = content.index(
        '<a class="card h-100 d-block text-decoration-none text-reset shadow-sm"'
    )
    card = content[card_start : content.index("</a>", card_start)]
    assert "<a " not in card[2:]
    assert "<button" not in card
