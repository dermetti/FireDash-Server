"""Phase 4B scope-centric Publications UI regressions."""

import time
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
    outsider = User.objects.create_user(
        "publication-outsider@example.test", "Outsider", "safe-password"
    )
    department = Department.objects.create(
        name="Publication UI", short_code="PUI", created_by=admin
    )
    other_department = Department.objects.create(
        name="Other UI", short_code="OUI", created_by=outsider
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="UI Station", short_code="UIS")
    return admin, outsider, department, other_department, station


def _publication(*, department, scope, version_number, status):
    publication_id = uuid.uuid4()
    ready = status in (DatasetPublication.Status.PUBLISHED, DatasetPublication.Status.SUPERSEDED)
    fields = {
        "id": publication_id,
        "department": department,
        "station": scope.station,
        "dataset_type_code": scope.dataset_type_code,
        "scope_state": scope,
        "version_number": version_number,
        "schema_version": 1,
        "source_revision": 1,
        "status": status,
    }
    if ready:
        fields.update(
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
    return DatasetPublication.objects.create(**fields)


def _current_scope(*, department, dataset_type_code="department_hydrants"):
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code=dataset_type_code
    )
    current = _publication(
        department=department,
        scope=scope,
        version_number=7,
        status=DatasetPublication.Status.PUBLISHED,
    )
    scope.latest_built_publication = current
    scope.current_published_publication = current
    scope.save(update_fields=("latest_built_publication", "current_published_publication"))
    return scope, current


@pytest.mark.django_db(transaction=True)
def test_primary_list_is_one_row_per_scope_with_current_version_and_detail_link(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    historical = _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    client.force_login(admin)

    response = client.get(reverse("publications-list", args=(department.id,)))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Dataset / Scope" in content
    assert "Current publication" in content
    assert "Update" in content
    assert "Last changed" in content
    assert content.count("department_hydrants") == 0
    assert content.count("Department hydrants") == 1
    assert f"v{current.version_number}" in content
    assert f"v{historical.version_number}" not in content
    assert reverse("publications-scope-detail", args=(scope.id,)) in content
    assert "View details" not in content
    assert "Previous versions" not in content


@pytest.mark.django_db(transaction=True)
def test_update_is_separate_from_current_and_building_row_polls_only_itself(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.STAGED,
    )
    job = PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=1,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
        status=PublicationJob.Status.RUNNING,
        build_publication=staged,
    )
    staged.status = DatasetPublication.Status.BUILDING
    staged.save(update_fields=("status",))
    client.force_login(admin)

    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert f"v{current.version_number}" in content
    assert "Current" in content
    assert "Building v8" in content
    assert 'hx-trigger="every 1s"' in content
    assert reverse("publications-scope-row", args=(scope.id,)) in content
    assert "Cancel build" in content
    assert "Delete staged" not in content

    job.status = PublicationJob.Status.FAILED
    job.save(update_fields=("status",))
    staged.status = DatasetPublication.Status.FAILED
    staged.save(update_fields=("status",))
    failed = client.get(reverse("publications-scope-row", args=(scope.id,))).content.decode()
    assert "v8 · Failed" in failed
    assert "every 1s" not in failed


@pytest.mark.django_db(transaction=True)
def test_staged_action_modal_is_scope_authorized_and_explains_history(
    client, publication_ui_context
):
    admin, outsider, department, _, _ = publication_ui_context
    scope, _ = _current_scope(department=department)
    staged = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.STAGED,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=1,
        trigger_type=PublicationJob.TriggerType.USER_REQUEST,
        status=PublicationJob.Status.PENDING,
        build_publication=staged,
    )
    modal_url = reverse("publications-lifecycle-modal", args=(staged.id, "delete-staged"))
    client.force_login(admin)

    list_content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Delete staged" in list_content
    assert "Roll back" not in list_content
    modal = client.get(modal_url, HTTP_HX_REQUEST="true")
    assert modal.status_code == 200
    assert "Delete staged publication v8?" in modal.content.decode()
    assert "not reused" in modal.content.decode()

    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()
    deleted = client.post(modal_url, HTTP_HX_REQUEST="true")
    assert deleted.status_code == 204
    assert deleted["HX-Redirect"] == reverse("publications-scope-detail", args=(scope.id,))
    staged.refresh_from_db()
    assert staged.status == DatasetPublication.Status.OBSOLETE

    client.force_login(outsider)
    assert client.get(modal_url, HTTP_HX_REQUEST="true").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_cancel_modal_reports_a_build_that_finished_first(client, publication_ui_context):
    admin, _, department, _, _ = publication_ui_context
    _scope, current = _current_scope(department=department)
    client.force_login(admin)
    session = client.session
    session["recent_reauthentication_at"] = time.time()
    session.save()

    response = client.post(
        reverse("publications-lifecycle-modal", args=(current.id, "cancel")),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert (
        "finished before cancellation could take effect and is now current"
        in response.content.decode()
    )


@pytest.mark.django_db(transaction=True)
def test_scope_detail_has_bounded_history_and_detail_only_successful_delete_actions(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, current = _current_scope(department=department)
    predecessor = _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    failed = _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.FAILED,
    )
    client.force_login(admin)

    list_content = client.get(reverse("publications-list", args=(department.id,))).content.decode()
    assert "Delete publication" not in list_content
    detail = client.get(reverse("publications-scope-detail", args=(scope.id,)))
    content = detail.content.decode()
    assert detail.status_code == 200
    assert "Department hydrants" in content
    assert "Version history" in content
    assert content.index("v8") < content.index("v7") < content.index("v6")
    assert "Roll back to this version" in content
    assert reverse("publications-lifecycle-modal", args=(predecessor.id, "rollback")) in content
    assert reverse("publications-lifecycle-modal", args=(current.id, "delete")) in content
    assert reverse("publications-lifecycle-modal", args=(failed.id, "rollback")) not in content


@pytest.mark.django_db(transaction=True)
def test_current_row_does_not_offer_rollback_when_only_newer_superseded_version_exists(
    client, publication_ui_context
):
    admin, _, department, _, _ = publication_ui_context
    scope, _ = _current_scope(department=department)
    _publication(
        department=department,
        scope=scope,
        version_number=8,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    client.force_login(admin)

    content = client.get(reverse("publications-list", args=(department.id,))).content.decode()

    assert "Roll back" not in content


@pytest.mark.django_db(transaction=True)
def test_filters_are_server_side_and_count_scopes_not_attempts(client, publication_ui_context):
    admin, _, department, _, station = publication_ui_context
    scope, _ = _current_scope(department=department)
    _publication(
        department=department,
        scope=scope,
        version_number=6,
        status=DatasetPublication.Status.SUPERSEDED,
    )
    mark_dirty(
        actor=admin,
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )
    client.force_login(admin)

    response = client.get(
        reverse("publications-list", args=(department.id,)),
        {"state": "scheduled"},
        HTTP_HX_REQUEST="true",
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert "Showing 1 of 1 publication scope" in content
    assert "Station personnel · UIS" in content
    assert "Department hydrants" not in content
