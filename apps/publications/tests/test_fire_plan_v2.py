import hashlib
import uuid

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications import artifacts
from apps.publications.builders import PublicationBuildError, build_source_payload
from apps.publications.fire_plan_v2 import build_fire_plan_v2_generation
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    FirePlanDocumentArtifact,
    PublicationFirePlanArtifactReference,
)
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan


@pytest.fixture
def v2_context(db, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    (tmp_path / "kek").write_bytes(b"k" * 32)
    (tmp_path / "signing").write_bytes(b"s" * 32)
    admin = User.objects.create_user("v2@example.test", "V2 Admin", "safe-password")
    department = Department.objects.create(name="V2 Dept", short_code="V2D", created_by=admin)
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    with override_settings(
        REFERENCE_DATA_ACCEPTED_ROOT=accepted_root,
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=1024,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="test-kek-v1",
        PUBLICATION_SIGNING_KEY_VERSION="test-signing-v1",
    ):
        yield admin, department, scope, accepted_root


def _plan(*, admin, department, accepted_root, identifier, pdf, active=True):
    plan = FirePlan.objects.create(
        department=department,
        external_identifier=identifier,
        object_name=f"Plan {identifier}",
        address=f"Street {identifier}",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename=f"{identifier}.pdf",
        file_size=len(pdf),
        page_count=1,
        sha256=hashlib.sha256(pdf).hexdigest(),
        active=active,
        uploaded_by=admin,
    )
    (accepted_root / plan.document_key).write_bytes(pdf)
    return plan


def _building_publication(*, department, scope, version):
    snapshot = build_source_payload(
        definition=get_dataset_definition("department_fire_plans"),
        department=department,
        station=None,
    )
    return DatasetPublication.objects.create(
        department=department,
        dataset_type_code="department_fire_plans",
        scope_state=scope,
        version_number=version,
        schema_version=1,
        source_revision=version,
        source_snapshot=snapshot,
        status=DatasetPublication.Status.BUILDING,
    )


@pytest.mark.django_db(transaction=True)
def test_v2_generation_is_complete_and_reuses_unchanged_artifacts(v2_context):
    admin, department, scope, accepted_root = v2_context
    first = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="A",
        pdf=b"PDF A",
    )
    second = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="B",
        pdf=b"PDF B",
    )
    generation_one = _building_publication(department=department, scope=scope, version=1)
    references_one = build_fire_plan_v2_generation(publication=generation_one)

    assert {reference.fire_plan_id for reference in references_one} == {first.id, second.id}
    assert (
        PublicationFirePlanArtifactReference.objects.filter(publication=generation_one).count() == 2
    )
    artifact_ids = {
        reference.fire_plan_id: reference.document_artifact_id for reference in references_one
    }

    generation_two = _building_publication(department=department, scope=scope, version=2)
    references_two = build_fire_plan_v2_generation(publication=generation_two)
    assert {
        reference.fire_plan_id: reference.document_artifact_id for reference in references_two
    } == artifact_ids
    assert FirePlanDocumentArtifact.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_v2_metadata_change_reuses_and_pdf_change_creates_one_artifact(v2_context):
    admin, department, scope, accepted_root = v2_context
    first = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="A",
        pdf=b"PDF A",
    )
    second = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="B",
        pdf=b"PDF B",
    )
    original = build_fire_plan_v2_generation(
        publication=_building_publication(department=department, scope=scope, version=1)
    )
    original_ids = {
        reference.fire_plan_id: reference.document_artifact_id for reference in original
    }

    first.object_name = "Renamed only"
    first.save(update_fields=("object_name",))
    metadata_generation = build_fire_plan_v2_generation(
        publication=_building_publication(department=department, scope=scope, version=2)
    )
    assert {
        reference.fire_plan_id: reference.document_artifact_id for reference in metadata_generation
    } == original_ids
    assert FirePlanDocumentArtifact.objects.count() == 2

    changed_pdf = b"PDF A changed"
    first.sha256 = hashlib.sha256(changed_pdf).hexdigest()
    first.file_size = len(changed_pdf)
    first.save(update_fields=("sha256", "file_size"))
    (accepted_root / first.document_key).write_bytes(changed_pdf)
    changed_generation = build_fire_plan_v2_generation(
        publication=_building_publication(department=department, scope=scope, version=3)
    )
    changed_ids = {
        reference.fire_plan_id: reference.document_artifact_id for reference in changed_generation
    }
    assert changed_ids[first.id] != original_ids[first.id]
    assert changed_ids[second.id] == original_ids[second.id]
    assert FirePlanDocumentArtifact.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_v2_generation_membership_is_exactly_the_frozen_snapshot(v2_context):
    admin, department, scope, accepted_root = v2_context
    first = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="A",
        pdf=b"PDF A",
    )
    second = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="B",
        pdf=b"PDF B",
    )
    generation_one = _building_publication(department=department, scope=scope, version=1)
    build_fire_plan_v2_generation(publication=generation_one)

    third = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="C",
        pdf=b"PDF C",
    )
    second.active = False
    second.save(update_fields=("active",))
    generation_two = _building_publication(department=department, scope=scope, version=2)
    references_two = build_fire_plan_v2_generation(publication=generation_two)
    assert {reference.fire_plan_id for reference in references_two} == {first.id, third.id}
    assert not PublicationFirePlanArtifactReference.objects.filter(
        publication=generation_two, fire_plan=second
    ).exists()
    assert FirePlanDocumentArtifact.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_v2_rejects_cancelled_and_stale_snapshots_without_generation_membership(v2_context):
    admin, department, scope, accepted_root = v2_context
    plan = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="A",
        pdf=b"PDF A",
    )
    changed_plan = _plan(
        admin=admin,
        department=department,
        accepted_root=accepted_root,
        identifier="B",
        pdf=b"PDF B",
    )
    cancelled = _building_publication(department=department, scope=scope, version=1)
    cancelled.status = DatasetPublication.Status.CANCELLED
    cancelled.save(update_fields=("status",))
    with pytest.raises(PublicationBuildError, match="building publication"):
        build_fire_plan_v2_generation(publication=cancelled)
    assert not PublicationFirePlanArtifactReference.objects.filter(publication=cancelled).exists()

    stale = _building_publication(department=department, scope=scope, version=2)
    changed_pdf = b"PDF B changed"
    changed_plan.sha256 = hashlib.sha256(changed_pdf).hexdigest()
    changed_plan.save(update_fields=("sha256",))
    (accepted_root / changed_plan.document_key).write_bytes(changed_pdf)
    with pytest.raises(PublicationBuildError, match="frozen metadata"):
        build_fire_plan_v2_generation(publication=stale)
    assert not PublicationFirePlanArtifactReference.objects.filter(publication=stale).exists()
    assert not PublicationFirePlanArtifactReference.objects.filter(fire_plan=plan).exists()
    assert stale.status == DatasetPublication.Status.BUILDING
