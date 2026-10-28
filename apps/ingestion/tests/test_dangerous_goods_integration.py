import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.ingestion.services import (
    ImportError,
    apply_dangerous_goods_preview,
    create_dangerous_goods_preview,
)
from apps.organizations.models import Department
from apps.publications.models import (
    DatasetPublication,
    DatasetScopeState,
    DatasetSourceRevision,
    PublicationJob,
)
from apps.publications.registry import get_dataset_definition
from apps.publications.services import process_next_job


def document(*, name="Name", placard="3"):
    return json.dumps(
        {
            "dataset_type": "dangerous_goods",
            "schema_version": 1,
            "metadata": {
                "publication_profile": "compact",
                "record_count": 1,
                "eri_card_count": 1,
                "placard_catalog": {
                    "available_assets": {"3": "ADR_3.svg", "7A": "a", "7B": "b", "7C": "c"},
                    "special_values": {
                        "7X": {"kind": "variable", "candidate_codes": ["7A", "7B", "7C"]}
                    },
                },
            },
            "goods": [
                {
                    "id": "bam-1",
                    "un_number": "1234",
                    "names": {"official": {"de": name}},
                    "adr": {"placards": [placard]},
                    "eri": ["3-01"],
                }
            ],
            "eri_defaults": {"1234": "3-01"},
            "eri_cards": {"3-01": [["title", "Title"]]},
            "sources": [
                {
                    "id": "bam",
                    "provider": "BAM",
                    "dataset": "ADR",
                    "source_file": "a",
                    "sha256": "a",
                    "source_url": "https://example.test",
                    "legal": {"legal_url": "x", "license": {}, "attribution": {}, "processing": {}},
                },
                {
                    "id": "ericards",
                    "provider": "Cefic",
                    "dataset": "ERI",
                    "source_file": "b",
                    "sha256": "b",
                    "source_url": "https://example.test",
                    "legal": {
                        "terms_url": "x",
                        "guidance_url": "x",
                        "disclaimer_url": "x",
                        "attribution": {},
                        "reproduction": {},
                    },
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


@pytest.fixture
def context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    user = User.objects.create_user("goods@example.test", "Goods", "password")
    department = Department.objects.create(name="Goods", short_code="GDS", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    SystemRole.objects.create(user=user)
    return user, department


@pytest.mark.django_db(transaction=True)
def test_apply_retains_exact_bytes_noops_and_allows_historical_reimport(context):
    user, department = context
    first, second = document(name="First"), document(name="Second")
    preview = create_dangerous_goods_preview(actor=user, filename="a.json", payload=first)
    apply_dangerous_goods_preview(actor=user, batch_id=preview.id)
    scope = DatasetScopeState.objects.get(department=None, dataset_type_code="dangerous_goods")
    retained = DatasetSourceRevision.objects.get(scope_state=scope, source_revision=1)
    assert bytes(retained.plaintext) == first
    assert retained.sha256 == hashlib.sha256(first).hexdigest()
    assert retained.byte_size == len(first)
    no_op = create_dangerous_goods_preview(actor=user, filename="same.json", payload=first)
    apply_dangerous_goods_preview(actor=user, batch_id=no_op.id)
    assert DatasetSourceRevision.objects.filter(scope_state=scope).count() == 1
    changed = create_dangerous_goods_preview(actor=user, filename="b.json", payload=second)
    apply_dangerous_goods_preview(actor=user, batch_id=changed.id)
    reverted = create_dangerous_goods_preview(actor=user, filename="a.json", payload=first)
    apply_dangerous_goods_preview(actor=user, batch_id=reverted.id)
    assert (
        list(
            DatasetSourceRevision.objects.filter(scope_state=scope).values_list("sha256", flat=True)
        )[-1]
        == hashlib.sha256(first).hexdigest()
    )


@pytest.mark.django_db(transaction=True)
def test_stale_apply_is_atomic_and_department_isolated(context):
    user, department = context
    other = Department.objects.create(name="Other", short_code="OTH", created_by=user)
    stale = create_dangerous_goods_preview(actor=user, filename="a.json", payload=document())
    current = create_dangerous_goods_preview(
        actor=user, filename="b.json", payload=document(name="New")
    )
    apply_dangerous_goods_preview(actor=user, batch_id=current.id)
    with pytest.raises(ImportError, match="re-preview"):
        apply_dangerous_goods_preview(actor=user, batch_id=stale.id)
    assert DatasetSourceRevision.objects.filter(scope_state__department__isnull=True).count() == 1
    assert not DatasetSourceRevision.objects.filter(scope_state__department=other).exists()


@pytest.mark.django_db(transaction=True)
def test_build_encrypts_and_publishes_exact_retained_source(
    context, settings, tmp_path, monkeypatch
):
    user, department = context
    payload = document()
    assert get_dataset_definition("dangerous_goods").required is True
    preview = create_dangerous_goods_preview(actor=user, filename="goods.json", payload=payload)
    apply_dangerous_goods_preview(actor=user, batch_id=preview.id)
    job = PublicationJob.objects.get(department=None, dataset_type_code="dangerous_goods")
    job.not_before = timezone.now()
    job.save(update_fields=("not_before",))
    kek, signing = b"k" * 32, b"s" * 32
    kek_path, signing_path = tmp_path / "kek", tmp_path / "signing"
    kek_path.write_bytes(base64.b64encode(kek))
    signing_path.write_bytes(base64.b64encode(signing))
    monkeypatch.setattr("apps.publications.artifacts.grp", None)
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "temp",
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_KEK_VERSION="test",
        PUBLICATION_SIGNING_KEY_VERSION="test",
    ):
        completed_job = process_next_job()
        assert completed_job is not None
        assert completed_job.status == PublicationJob.Status.SUCCEEDED, completed_job.error_message
        publication = DatasetPublication.objects.get(
            department=None, dataset_type_code="dangerous_goods"
        )
        ciphertext = (tmp_path / "artifacts" / publication.artifact_path).read_bytes()
        assert publication.artifact_wrapped_cek is not None
        cek = aes_key_unwrap(kek, bytes(publication.artifact_wrapped_cek))
        assert publication.artifact_nonce is not None
        assert AESGCM(cek).decrypt(bytes(publication.artifact_nonce), ciphertext, None) == payload
        from apps.publications.services import delete_publication, rollback_publication

        next_preview = create_dangerous_goods_preview(
            actor=user, filename="next.json", payload=document(name="Next")
        )
        apply_dangerous_goods_preview(actor=user, batch_id=next_preview.id)
        next_job = PublicationJob.objects.get(scope_type="SYSTEM", status="PENDING")
        next_job.not_before = timezone.now()
        next_job.save(update_fields=("not_before",))
        assert process_next_job().status == PublicationJob.Status.SUCCEEDED
        current = DatasetPublication.objects.get(
            scope_state=publication.scope_state, status="PUBLISHED"
        )
        assert current.version_number == 2
        rollback_publication(actor=user, publication=publication)
        publication.refresh_from_db()
        current.refresh_from_db()
        assert publication.status == DatasetPublication.Status.PUBLISHED
        assert current.status == DatasetPublication.Status.SUPERSEDED
        delete_publication(actor=user, publication=current)
        current.refresh_from_db()
        assert current.status == DatasetPublication.Status.OBSOLETE
    assert publication.status == DatasetPublication.Status.PUBLISHED
    assert publication.artifact_status == DatasetPublication.ArtifactStatus.READY


@pytest.mark.django_db(transaction=True)
def test_department_admin_denied_ingestion_and_publication_services(context):
    from django.core.exceptions import PermissionDenied

    from apps.authorization.services import require_publication_scope

    admin, department = context
    outsider = User.objects.create_user("department-only@example.test", "Department", "password")
    DepartmentMembership.objects.create(user=outsider, department=department, created_by=admin)
    preview = create_dangerous_goods_preview(actor=admin, filename="goods.json", payload=document())
    with pytest.raises(PermissionDenied):
        create_dangerous_goods_preview(actor=outsider, filename="goods.json", payload=document())
    with pytest.raises(PermissionDenied):
        apply_dangerous_goods_preview(actor=outsider, batch_id=preview.id)
    with pytest.raises(PermissionDenied):
        require_publication_scope(
            outsider, department=None, station=None, dataset_type_code="dangerous_goods"
        )
    require_publication_scope(
        admin, department=None, station=None, dataset_type_code="dangerous_goods"
    )


@pytest.mark.django_db(transaction=True)
def test_import_batch_owner_constraint(context):
    from django.db import IntegrityError, transaction

    from apps.ingestion.models import ImportBatch

    admin, department = context
    common = dict(
        actor=admin,
        import_format="json",
        import_mode="merge",
        original_filename="x",
        upload_sha256="x",
        staging_key="invalid-owner",
    )
    for domain, owner in (("dangerous_goods", department), ("hydrants", None)):
        with pytest.raises(IntegrityError), transaction.atomic():
            ImportBatch.objects.create(domain=domain, department=owner, **common)


@pytest.mark.django_db(transaction=True)
def test_department_admin_cannot_use_system_publication_lifecycle(context):
    from django.core.exceptions import PermissionDenied

    from apps.publications.services import (
        build_staged_publication,
        cancel_publication_build,
        delete_publication,
        delete_staged_publication,
        publish_publication,
        request_rebuild,
        rollback_publication,
        stage_publication_update,
    )

    admin, department = context
    outsider = User.objects.create_user("lifecycle-denied@example.test", "Department", "password")
    DepartmentMembership.objects.create(user=outsider, department=department, created_by=admin)
    preview = create_dangerous_goods_preview(actor=admin, filename="goods.json", payload=document())
    apply_dangerous_goods_preview(actor=admin, batch_id=preview.id)
    scope = DatasetScopeState.objects.get(scope_type="SYSTEM", dataset_type_code="dangerous_goods")
    publication = DatasetPublication.objects.get(scope_state=scope, version_number=1)
    for operation in (
        publish_publication,
        cancel_publication_build,
        delete_publication,
        delete_staged_publication,
        rollback_publication,
    ):
        with pytest.raises(PermissionDenied):
            operation(actor=outsider, publication=publication)
    with pytest.raises(PermissionDenied):
        build_staged_publication(actor=outsider, scope=scope)
    for operation in (request_rebuild, stage_publication_update):
        with pytest.raises(PermissionDenied):
            operation(actor=outsider, department=None, dataset_type_code="dangerous_goods")
