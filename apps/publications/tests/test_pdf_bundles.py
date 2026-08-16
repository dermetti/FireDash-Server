import base64
import hashlib
import io
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.hazmat.primitives import keywrap
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import override_settings

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications import artifacts
from apps.publications.artifacts import _signature_payload, build_encrypted_artifact
from apps.publications.builders import build_summary
from apps.publications.features import get_feature_definition
from apps.publications.pdf_bundles import (
    AcceptedPdfBundleDocument,
    PdfBundleError,
    build_pdf_bundle_v1,
    document_archive_path,
    validate_pdf_bundle_v1,
)
from apps.publications.registry import get_dataset_definition


def _document(*, document_id: uuid.UUID, key: str, pdf: bytes) -> AcceptedPdfBundleDocument:
    return AcceptedPdfBundleDocument(
        id=document_id,
        title="KLGV Site Plan",
        document_key=key,
        sha256=hashlib.sha256(pdf).hexdigest(),
        page_count=1,
        category="plan",
    )


def _write_bundle_source(root: Path, *, document_id: uuid.UUID | None = None):
    root.mkdir(parents=True, exist_ok=True)
    document_id = document_id or uuid.UUID("11111111-1111-1111-1111-111111111111")
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    key = f"{document_id}.pdf"
    (root / key).write_bytes(pdf)
    return _document(document_id=document_id, key=key, pdf=pdf), pdf


def test_klgv_registry_is_department_scoped_optional_and_internal():
    definition = get_dataset_definition("department_klgv_plans")

    assert definition.scope == "department"
    assert definition.required is False
    assert definition.current_schema_version == 1
    assert definition.artifact_format == "zip"
    assert definition.internal_only is True
    assert definition.feature_code == "klgv_plans"
    assert get_feature_definition("klgv_plans").default_enabled is False


@pytest.mark.django_db
def test_klgv_builder_uses_the_canonical_klgv_source_model():
    definition = get_dataset_definition("department_klgv_plans")
    department = Department.objects.create(
        name="KLGV source",
        short_code="KLGV",
        created_by=User.objects.create_user(
            "klgv-source@example.test", "KLGV source", "safe-password"
        ),
    )
    assert build_summary(
        definition=definition,
        department=department,
        station=None,
        source_revision=1,
    ) == {
        "document_count": 0,
        "total_accepted_bytes": 0,
        "total_pages": 0,
        "source_revision": 1,
    }


def test_pdf_bundle_v1_has_deterministic_schema_paths_hashes_and_order(tmp_path: Path):
    root = tmp_path / "accepted"
    second, _ = _write_bundle_source(
        root, document_id=uuid.UUID("22222222-2222-2222-2222-222222222222")
    )
    first, first_pdf = _write_bundle_source(
        root, document_id=uuid.UUID("11111111-1111-1111-1111-111111111111")
    )

    first_bundle = build_pdf_bundle_v1(
        documents=[second, first], source_revision=9, accepted_root=root
    )
    second_bundle = build_pdf_bundle_v1(
        documents=[first, second], source_revision=9, accepted_root=root
    )

    assert first_bundle == second_bundle
    manifest = validate_pdf_bundle_v1(first_bundle)
    assert manifest["schema_version"] == 1
    assert manifest["source_revision"] == 9
    assert [entry["id"] for entry in manifest["documents"]] == [str(first.id), str(second.id)]
    assert manifest["documents"][0]["path"] == document_archive_path(first.id)
    assert manifest["documents"][0]["sha256"] == hashlib.sha256(first_pdf).hexdigest()
    assert manifest["documents"][0]["page_count"] == 1


def test_pdf_bundle_rejects_duplicate_documents_unsafe_sources_and_bad_hashes(tmp_path: Path):
    root = tmp_path / "accepted"
    document, _ = _write_bundle_source(root)
    duplicate = AcceptedPdfBundleDocument(
        id=document.id,
        title="Duplicate",
        document_key=document.document_key,
        sha256=document.sha256,
        page_count=1,
    )
    with pytest.raises(PdfBundleError, match="duplicate document IDs"):
        build_pdf_bundle_v1(documents=[document, duplicate], source_revision=1, accepted_root=root)
    unsafe = AcceptedPdfBundleDocument(
        id=uuid.uuid4(),
        title="Unsafe",
        document_key="../outside.pdf",
        sha256=document.sha256,
        page_count=1,
    )
    with pytest.raises(PdfBundleError, match="unsafe"):
        build_pdf_bundle_v1(documents=[unsafe], source_revision=1, accepted_root=root)
    bad_hash = AcceptedPdfBundleDocument(
        id=uuid.uuid4(),
        title="Bad hash",
        document_key=document.document_key,
        sha256="0" * 64,
        page_count=1,
    )
    with pytest.raises(PdfBundleError, match="hash does not match"):
        build_pdf_bundle_v1(documents=[bad_hash], source_revision=1, accepted_root=root)


def test_pdf_bundle_validator_rejects_undeclared_and_traversal_members(tmp_path: Path):
    root = tmp_path / "accepted"
    document, _ = _write_bundle_source(root)
    bundle = build_pdf_bundle_v1(documents=[document], source_revision=1, accepted_root=root)
    with zipfile.ZipFile(io.BytesIO(bundle), "r") as archive:
        manifest = archive.read("manifest.json")
        pdf = archive.read(document_archive_path(document.id))
    for extra_name, expected_error in (
        ("documents/extra.pdf", "undeclared"),
        ("../escape.pdf", "unsafe"),
    ):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest)
            archive.writestr(document_archive_path(document.id), pdf)
            archive.writestr(extra_name, b"not a PDF")
        with pytest.raises(PdfBundleError, match=expected_error):
            validate_pdf_bundle_v1(output.getvalue())


def test_pdf_bundle_plaintext_uses_existing_encryption_and_artifact_signature(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "accepted"
    document, _ = _write_bundle_source(root)
    plaintext = build_pdf_bundle_v1(documents=[document], source_revision=1, accepted_root=root)
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    kek, signing_seed = b"k" * 32, b"s" * 32
    (tmp_path / "kek").write_bytes(base64.b64encode(kek))
    (tmp_path / "signing").write_bytes(signing_seed)
    publication = SimpleNamespace(
        id="publication-id",
        department_id="department-id",
        station_id=None,
        dataset_type_code="department_klgv_plans",
        schema_version=1,
        version_number=4,
    )
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "artifacts" / ".tmp",
        PUBLICATION_ARTIFACT_MAX_BYTES=10_000,
        PUBLICATION_KEK_CREDENTIAL_PATH=tmp_path / "kek",
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=tmp_path / "signing",
        PUBLICATION_KEK_VERSION="1",
        PUBLICATION_SIGNING_KEY_VERSION="1",
    ):
        metadata = build_encrypted_artifact(publication=publication, plaintext=plaintext)
        ciphertext = (tmp_path / "artifacts" / cast(str, metadata["artifact_path"])).read_bytes()
        signature = cast(bytes, metadata["artifact_signature"])
        wrapped_cek = cast(bytes, metadata["artifact_wrapped_cek"])
        nonce = cast(bytes, metadata["artifact_nonce"])
        Ed25519PrivateKey.from_private_bytes(signing_seed).public_key().verify(
            signature,
            _signature_payload(
                publication=publication,
                wrapped_cek=wrapped_cek,
                nonce=nonce,
                ciphertext=ciphertext,
            ),
        )
        cek = keywrap.aes_key_unwrap(kek, wrapped_cek)
        assert AESGCM(cek).decrypt(nonce, ciphertext, None) == plaintext
