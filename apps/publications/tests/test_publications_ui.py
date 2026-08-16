"""Publications operational UI and HTMX polling regression tests."""

import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
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


def _published(*, department, scope, version_number=7):
    publication_id = uuid.uuid4()
    return DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        station=scope.station,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
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
        "Publication status",
        "Current publications",
        "Attention / failures",
        "Previous versions",
    ):
        assert heading in content
    assert "v7" in content
    assert "Safe validation failure." in content
    assert "Build &amp; publish now" in content
    assert "Source revision" not in content
    assert "Ready to publish" not in content
    assert "Reject" not in content
    assert "Building / publishing" not in content
    assert content.index("Publication status") < content.index("Scheduled updates")
    assert content.index("Scheduled updates") < content.index("Current publications")
    assert content.index("Current publications") < content.index("Attention / failures")
    assert content.index("Attention / failures") < content.index("Previous versions")


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
    assert "Publication status" in content
    assert "Scopes" in content
    assert "Scheduled updates" in content


@pytest.mark.django_db(transaction=True)
def test_current_publication_remains_visible_with_replacement_states(
    client, publication_ui_context
):
    admin, department, _station = publication_ui_context
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    current = _published(department=department, scope=scope, version_number=7)
    scope.latest_built_publication = current
    scope.current_published_publication = current
    scope.save(update_fields=("latest_built_publication", "current_published_publication"))
    client.force_login(admin)

    mark_dirty(
        actor=admin,
        department=department,
        station=None,
        dataset_type_code=scope.dataset_type_code,
    )
    url = reverse("publications-list", args=(department.id,))
    scheduled = client.get(url).content.decode()
    assert "v7" in scheduled
    assert "Update scheduled" in scheduled

    job = PublicationJob.objects.get(scope_state=scope, status=PublicationJob.Status.PENDING)
    job.status = PublicationJob.Status.RUNNING
    job.save(update_fields=("status",))
    building = client.get(url).content.decode()
    assert "v7" in building
    assert "Building update" in building

    failed = DatasetPublication.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=8,
        schema_version=1,
        source_revision=2,
        status=DatasetPublication.Status.FAILED,
        build_error="Safe replacement failure.",
    )
    job.status = PublicationJob.Status.FAILED
    job.save(update_fields=("status",))
    scope.latest_built_publication = failed
    scope.save(update_fields=("latest_built_publication",))
    failed_page = client.get(url).content.decode()
    assert "v7" in failed_page
    assert "Update failed" in failed_page
    assert "Safe replacement failure." in failed_page
    assert "Known-good publication v7 remains active." in failed_page

    replacement = _published(department=department, scope=scope, version_number=9)
    current.status = DatasetPublication.Status.SUPERSEDED
    current.save(update_fields=("status",))
    scope.latest_built_publication = replacement
    scope.current_published_publication = replacement
    scope.dirty_since = None
    scope.save(
        update_fields=("latest_built_publication", "current_published_publication", "dirty_since")
    )
    promoted = client.get(url).content.decode()
    assert "v9" in promoted
    assert "v7" in promoted
    assert "Update failed" not in promoted
    assert "Previous versions" in promoted
