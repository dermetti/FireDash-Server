"""Shared PDF boundary and immutable replacement with real synthetic documents."""

# ruff: noqa: F811
import hashlib
import io
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pikepdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.file_library import services as library_services
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.ingestion import services
from apps.ingestion.models import ImportBatch
from apps.publications import artifacts
from apps.publications.builders import build_source_payload
from apps.publications.document_v2 import build_document_v2_generation
from apps.publications.feature_services import set_department_feature
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan, KlgvPlan
from apps.reference_data.pdf_sandbox import PdfSanitizerError
from apps.reference_data.pdf_sandbox import sanitize as broker_sanitize
from apps.reference_data.pdf_validation import PdfValidationError

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["library", "fire_plans", "klgv_plans"])
def workflow(request, scope, pdf_pipeline, settings, monkeypatch):
    settings.INGESTION_STAGING_ROOT = pdf_pipeline / "staging"
    actor, department = scope[0], scope[3]
    kind = request.param
    if kind == "klgv_plans":
        set_department_feature(actor=actor, department=department, feature_code=kind, enabled=True)
    library = library_for(scope) if kind == "library" else None
    calls = []
    original = library_services.storage.pdf_sandbox.sanitize

    def worker(**kwargs):
        calls.append(kwargs["strict"])
        return original(**kwargs)

    monkeypatch.setattr(library_services.storage.pdf_sandbox, "sanitize", worker)

    def preview(pages=1, upload=None):
        upload = upload if upload is not None else pdf(pages=pages)
        return services.create_single_preview(
            actor=actor,
            department=department,
            domain=kind,
            values={
                "external_identifier": "P-1",
                "object_name": "Plan",
                "address": "Street 1",
                "postal_code": "12345",
                "city": "Town",
                "longitude": "",
                "latitude": "",
            },
            pdf_bytes=upload.read(),
            original_filename=upload.name,
        )

    def save(pages=1, current=None, upload=None):
        if kind == "library":
            assert library is not None
            return library_services.upload_pdf(
                actor=actor,
                library_id=library.id,
                upload=upload if upload is not None else pdf(pages=pages),
                replace_id=current.pk if current else None,
            )
        batch = preview(pages, upload=upload)
        if current:
            services.approve_all_review_decisions(actor=actor, batch_id=batch.id)
        services.apply_preview(actor=actor, batch_id=batch.id)
        model = FirePlan if kind == "fire_plans" else KlgvPlan
        return model.objects.get(department=department, external_identifier="P-1")

    def key(document):
        if kind == "library":
            return document.content.storage_key
        return document.document_key if kind == "fire_plans" else document.path

    return kind, actor, department, save, preview, key, calls


def test_shared_pipeline_and_fresh_identity_on_replacement(workflow, settings):
    kind, actor, department, save, _, key, calls = workflow
    first = save()
    old_key = key(first)
    old = settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key
    original = old.read_bytes()
    replacement = save(pages=2, current=first)
    assert replacement.pk == first.pk
    assert key(replacement) != old_key
    assert old.read_bytes() == original  # uncommitted/on-commit cleanup must not alter it
    new = settings.REFERENCE_DATA_ACCEPTED_ROOT / key(replacement)
    assert new.read_bytes() != original
    digest = replacement.content.sha256 if kind == "library" else replacement.sha256
    assert hashlib.sha256(new.read_bytes()).hexdigest() == digest
    assert calls == ([True, True] if kind == "library" else [False, False])


def test_database_failure_preserves_previous_bytes_and_reference(workflow, settings, monkeypatch):
    kind, actor, department, save, _, key, _ = workflow
    first = save()
    old_key = key(first)
    old = settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key
    content = old.read_bytes()
    files = set(settings.REFERENCE_DATA_ACCEPTED_ROOT.rglob("*.pdf"))
    module = library_services if kind == "library" else services
    record = module.record_event

    def fail_after_promotion(**kwargs):
        if kwargs["action"] in {"file_library.replaced", "ingestion.applied"}:
            raise RuntimeError("audit rollback")
        return record(**kwargs)

    monkeypatch.setattr(module, "record_event", fail_after_promotion)
    with pytest.raises(RuntimeError, match="audit rollback"):
        save(pages=2, current=first)
    first.refresh_from_db()
    assert key(first) == old_key and old.read_bytes() == content
    assert set(settings.REFERENCE_DATA_ACCEPTED_ROOT.rglob("*.pdf")) == files
    if kind != "library":
        batch = ImportBatch.objects.filter(status=ImportBatch.Status.PREVIEW_READY).latest(
            "created_at"
        )
        row = batch.normalized_intent["rows"][0]
        staged = settings.INGESTION_STAGING_ROOT / row["sanitized_staging_key"]
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == row["sanitized_pdf_sha256"]


@pytest.mark.django_db(transaction=True)
def test_post_commit_failure_preserves_committed_content(workflow, settings, monkeypatch):
    kind, _, _, save, _, key, _ = workflow
    first = save()
    old_key = key(first)
    module = library_services if kind == "library" else services
    record = module.record_event

    def callback_failure():
        raise RuntimeError("post-commit failure")

    def queue_failure(**kwargs):
        result = record(**kwargs)
        if kwargs["action"] in {"file_library.replaced", "ingestion.applied"}:
            transaction.on_commit(callback_failure)
        return result

    monkeypatch.setattr(module, "record_event", queue_failure)
    with pytest.raises(RuntimeError, match="post-commit failure"):
        save(pages=2, current=first)
    first.refresh_from_db()
    assert key(first) != old_key
    path = settings.REFERENCE_DATA_ACCEPTED_ROOT / key(first)
    expected = first.content.sha256 if kind == "library" else first.sha256
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def test_sandbox_is_mandatory_for_each_workflow(workflow, monkeypatch):
    _, _, _, save, _, _, _ = workflow
    worker = Mock(side_effect=PdfSanitizerError("broker unavailable"))
    monkeypatch.setattr(library_services.storage.pdf_sandbox, "sanitize", worker)
    with pytest.raises((PdfSanitizerError, services.ImportError)):
        save()
    worker.assert_called_once()
    assert not FirePlan.objects.exists() and not KlgvPlan.objects.exists()


def test_web_pipeline_never_invokes_native_inspection(workflow, settings, tmp_path, monkeypatch):
    _, _, _, save, _, _, _ = workflow
    sandbox = library_services.storage.pdf_sandbox
    monkeypatch.setattr(sandbox, "sanitize", broker_sanitize)
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="linux"))
    socket_path = tmp_path / "broker.sock"
    socket_path.touch()
    settings.PDF_SANITIZER_BROKER_SOCKET = str(socket_path)

    def broker_reply(socket_path, job, timeout):
        source = settings.REFERENCE_DATA_QUARANTINE_ROOT / job / "input.pdf"
        output = settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT / job / "sanitized.pdf"
        shutil.copyfile(source, output)
        return f"OK {output.stat().st_size} 1 {hashlib.sha256(output.read_bytes()).hexdigest()}"

    monkeypatch.setattr(sandbox, "_request_sanitization", broker_reply)
    inspect = Mock(side_effect=AssertionError("native parser in web process"))
    monkeypatch.setattr("apps.reference_data.pdf_validation._inspect_pdf", inspect)
    monkeypatch.setattr("apps.reference_data.pdf_validation._validate_mime", inspect)
    assert save().pk
    inspect.assert_not_called()


def test_unsafe_name_action_cannot_replace_any_document(workflow, settings):
    kind, _, _, save, _, key, _ = workflow
    first = save()
    old_key = key(first)
    original = (settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key).read_bytes()
    payload = io.BytesIO()
    with pikepdf.new() as document:
        document.add_blank_page()
        document.Root["/OpenAction"] = pikepdf.Dictionary(S=pikepdf.Name("/Launch"))
        document.save(payload)
    upload = SimpleUploadedFile("unsafe.pdf", payload.getvalue())
    with pytest.raises((PdfValidationError, services.ImportError)):
        save(current=first, upload=upload)
    first.refresh_from_db()
    assert key(first) == old_key
    assert (settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key).read_bytes() == original


@pytest.mark.parametrize("workflow", ["fire_plans", "klgv_plans"], indirect=True)
def test_tampered_staged_preview_cannot_replace_existing(workflow, settings):
    kind, actor, department, save, preview, key, _ = workflow
    first = save()
    old_key = key(first)
    old = (settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key).read_bytes()
    batch = preview(2)
    services.approve_all_review_decisions(actor=actor, batch_id=batch.id)
    staged = (
        settings.INGESTION_STAGING_ROOT
        / batch.normalized_intent["rows"][0]["sanitized_staging_key"]
    )
    staged.write_bytes(b"tampered")
    with pytest.raises(services.ImportError, match="preview"):
        services.apply_preview(actor=actor, batch_id=batch.id)
    first.refresh_from_db()
    assert key(first) == old_key
    assert (settings.REFERENCE_DATA_ACCEPTED_ROOT / old_key).read_bytes() == old


@pytest.mark.parametrize("workflow", ["fire_plans", "klgv_plans"], indirect=True)
def test_retained_plan_publication_survives_replacement(
    workflow, settings, monkeypatch, tmp_path, django_capture_on_commit_callbacks
):
    kind, actor, department, save, _, key, _ = workflow
    first = save()
    old = settings.REFERENCE_DATA_ACCEPTED_ROOT / key(first)
    old_bytes = old.read_bytes()
    code = "department_" + kind
    scope_state = DatasetScopeState.objects.get(department=department, dataset_type_code=code)
    snapshot = build_source_payload(
        definition=get_dataset_definition(code), department=department, station=None
    )
    publication = DatasetPublication.objects.create(
        department=department,
        dataset_type_code=code,
        scope_state=scope_state,
        version_number=2,
        schema_version=2,
        source_revision=scope_state.source_revision,
        source_snapshot=snapshot,
        status=DatasetPublication.Status.BUILDING,
    )
    settings.PUBLICATION_ARTIFACT_ROOT = tmp_path / "artifacts"
    settings.PUBLICATION_ARTIFACT_TEMP_ROOT = tmp_path / "artifact-tmp"
    settings.PUBLICATION_KEK_CREDENTIAL_PATH = tmp_path / "disposable-kek"
    settings.PUBLICATION_KEK_CREDENTIAL_PATH.write_bytes(b"k" * 32)
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH = tmp_path / "disposable-signing"
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH.write_bytes(b"s" * 32)
    monkeypatch.setattr(artifacts, "_set_final_artifact_permissions", lambda path: None)
    monkeypatch.setattr(artifacts, "_set_final_directory_permissions", lambda path: None)
    reference = build_document_v2_generation(publication=publication)[0]
    artifact = reference.document_artifact
    encrypted = settings.PUBLICATION_ARTIFACT_ROOT / artifact.artifact_path
    ciphertext = encrypted.read_bytes()
    with django_capture_on_commit_callbacks(execute=True):
        save(pages=2, current=first)
    assert old.read_bytes() == old_bytes
    reference.refresh_from_db()
    assert reference.document_artifact_id == artifact.id
    assert encrypted.read_bytes() == ciphertext
    assert hashlib.sha256(ciphertext).hexdigest() == artifact.ciphertext_sha256
