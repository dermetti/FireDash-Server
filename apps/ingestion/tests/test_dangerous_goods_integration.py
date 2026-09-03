import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
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
    return user, department


@pytest.mark.django_db(transaction=True)
def test_apply_retains_exact_bytes_noops_and_allows_historical_reimport(context):
    user, department = context
    first, second = document(name="First"), document(name="Second")
    preview = create_dangerous_goods_preview(
        actor=user, department=department, filename="a.json", payload=first
    )
    apply_dangerous_goods_preview(actor=user, batch_id=preview.id)
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="dangerous_goods"
    )
    retained = DatasetSourceRevision.objects.get(scope_state=scope, source_revision=1)
    assert bytes(retained.plaintext) == first
    assert retained.sha256 == hashlib.sha256(first).hexdigest()
    assert retained.byte_size == len(first)
    no_op = create_dangerous_goods_preview(
        actor=user, department=department, filename="same.json", payload=first
    )
    apply_dangerous_goods_preview(actor=user, batch_id=no_op.id)
    assert DatasetSourceRevision.objects.filter(scope_state=scope).count() == 1
    changed = create_dangerous_goods_preview(
        actor=user, department=department, filename="b.json", payload=second
    )
    apply_dangerous_goods_preview(actor=user, batch_id=changed.id)
    reverted = create_dangerous_goods_preview(
        actor=user, department=department, filename="a.json", payload=first
    )
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
    stale = create_dangerous_goods_preview(
        actor=user, department=department, filename="a.json", payload=document()
    )
    current = create_dangerous_goods_preview(
        actor=user, department=department, filename="b.json", payload=document(name="New")
    )
    apply_dangerous_goods_preview(actor=user, batch_id=current.id)
    with pytest.raises(ImportError, match="re-preview"):
        apply_dangerous_goods_preview(actor=user, batch_id=stale.id)
    assert DatasetSourceRevision.objects.filter(scope_state__department=department).count() == 1
    assert not DatasetSourceRevision.objects.filter(scope_state__department=other).exists()


@pytest.mark.django_db(transaction=True)
def test_build_encrypts_and_publishes_exact_retained_source(
    context, settings, tmp_path, monkeypatch
):
    user, department = context
    payload = document()
    assert get_dataset_definition("dangerous_goods").required is True
    preview = create_dangerous_goods_preview(
        actor=user, department=department, filename="goods.json", payload=payload
    )
    apply_dangerous_goods_preview(actor=user, batch_id=preview.id)
    job = PublicationJob.objects.get(department=department, dataset_type_code="dangerous_goods")
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
        assert completed_job.status == PublicationJob.Status.SUCCEEDED, completed_job.error_message
        publication = DatasetPublication.objects.get(
            department=department, dataset_type_code="dangerous_goods"
        )
        ciphertext = (tmp_path / "artifacts" / publication.artifact_path).read_bytes()
        cek = aes_key_unwrap(kek, bytes(publication.artifact_wrapped_cek))
        assert AESGCM(cek).decrypt(bytes(publication.artifact_nonce), ciphertext, None) == payload
    assert publication.status == DatasetPublication.Status.PUBLISHED
    assert publication.artifact_status == DatasetPublication.ArtifactStatus.READY
