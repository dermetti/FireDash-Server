"""Publications operational UI and HTMX polling regression tests."""

import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.publications.paths import publication_artifact_relative_path
from apps.publications.services import mark_dirty


@pytest.fixture
def publication_ui_context(db):
    admin = User.objects.create_user("publication-ui@example.test", "UI Admin", "safe-password")
    department = Department.objects.create(
        name="Publication UI", short_code="PUI", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="UI Station", short_code="UIS")
    return admin, department, station


def _published(*, department, scope):
    publication_id = uuid.uuid4()
    return DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        station=scope.station,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=7,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path=publication_artifact_relative_path(
            department_id=department.id, publication_id=publication_id
        ),
        artifact_size=1,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"w" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="test",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="test",
    )


@pytest.mark.django_db(transaction=True)
def test_publications_page_has_operational_sections_and_hides_review_workflow(
    client, publication_ui_context
):
    admin, department, station = publication_ui_context
    current_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    current = _published(department=department, scope=current_scope)
    current_scope.latest_built_publication = current
    current_scope.current_published_publication = current
    current_scope.save(update_fields=("latest_built_publication", "current_published_publication"))

    failed_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    failed = DatasetPublication.objects.create(
        department=department,
        dataset_type_code=failed_scope.dataset_type_code,
        scope_state=failed_scope,
        version_number=8,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.FAILED,
        build_error="Safe validation failure.",
    )
    failed_scope.latest_built_publication = failed
    failed_scope.save(update_fields=("latest_built_publication",))
    mark_dirty(
        actor=admin,
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )

    client.force_login(admin)
    response = client.get(reverse("publications-list", args=(department.id,)))

    assert response.status_code == 200
    content = response.content.decode()
    for heading in (
        "Scheduled updates",
        "Building / publishing",
        "Attention / failures",
        "Current publications",
        "History",
    ):
        assert heading in content
    assert "v7" in content
    assert "Safe validation failure." in content
    assert "Build &amp; publish now" in content
    assert "Source revision" not in content
    assert "Ready to publish" not in content
    assert "Reject" not in content


@pytest.mark.django_db(transaction=True)
def test_publication_status_partial_is_htmx_pollable_and_authorized(client, publication_ui_context):
    admin, department, station = publication_ui_context
    mark_dirty(
        actor=admin,
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )
    client.force_login(admin)
    response = client.get(
        reverse("publications-status", args=(department.id,)),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="publication-status"' in content
    assert "hx-get=" in content
    assert "every 5s" in content
    assert "Scheduled updates" in content
