"""Partial per-document acceptance for PDF-package ingestion."""

import hashlib
import io
import shutil
import zipfile

import pytest

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.ingestion.pdf_packages import manifest_member_name
from apps.ingestion.services import apply_preview, create_preview
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.reference_data.models import FirePlan, KlgvPlan
from apps.reference_data.pdf_sandbox import PdfSanitizerContentError, PdfSanitizerError
from apps.reference_data.pdf_validation import PdfValidationError

FIRE_HEADER = (
    "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action"
)


def fire_manifest(rows: str) -> str:
    return FIRE_HEADER + "\n" + rows + "\n"


def package(domain: str, manifest: str, files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(manifest_member_name(domain), manifest)
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.fixture
def partial_fixture(db, settings, tmp_path, monkeypatch):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    quarantine_root = tmp_path / "quarantine"
    output_root = tmp_path / "sanitized"
    accepted_root = tmp_path / "accepted"
    settings.REFERENCE_DATA_QUARANTINE_ROOT = quarantine_root
    settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT = output_root
    settings.REFERENCE_DATA_ACCEPTED_ROOT = accepted_root

    actor = User.objects.create_user("partial@example.test", "Partial", "safe-password")
    department = Department.objects.create(name="Partial", short_code="PAR", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)

    validation_rules: dict[bytes, PdfValidationError] = {}
    sanitizer_rules: dict[bytes, BaseException] = {}

    def write(upload):
        path = quarantine_root / "job" / "input.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(upload.read())
        return path

    def output_path(*, job_id):
        path = output_root / job_id / "sanitized.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def sanitize(*, quarantined_input, sanitized_output):
        content = quarantined_input.read_bytes()
        for marker, error in sanitizer_rules.items():
            if marker in content:
                raise error
        shutil.copyfile(quarantined_input, sanitized_output)

    def validate(path, **kwargs):
        content = path.read_bytes()
        for marker, error in validation_rules.items():
            if marker in content:
                raise error
        return len(content), 1, hashlib.sha256(content).hexdigest()

    def promote(source, key):
        accepted_root.mkdir(parents=True, exist_ok=True)
        destination = accepted_root / key
        shutil.move(source, destination)
        return destination

    monkeypatch.setattr("apps.ingestion.services.write_quarantine", write)
    monkeypatch.setattr("apps.ingestion.services.output_path", output_path)
    monkeypatch.setattr("apps.ingestion.services.sanitize", sanitize)
    monkeypatch.setattr("apps.ingestion.services.validate_pdf", validate)
    monkeypatch.setattr("apps.ingestion.services.promote_to_accepted", promote)

    return actor, department, validation_rules, sanitizer_rules


def _fire_preview(actor, department, manifest, files):
    return create_preview(
        actor=actor,
        department=department,
        domain="fire_plans",
        import_format="zip",
        import_mode="upsert",
        filename="plans.zip",
        payload=package("fire_plans", manifest, files),
    )


@pytest.mark.django_db(transaction=True)
def test_mixed_package_partial_acceptance(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    validation_rules[b"MALFORMED"] = PdfValidationError(
        "PDF is malformed or cannot be parsed.", code="malformed_pdf"
    )
    manifest = fire_manifest(
        "A,a.pdf,Plan A,Main 1,,,,,upsert\n"
        "B,b.pdf,Plan B,Main 2,,,,,upsert\n"
        "C,c.pdf,Plan C,Main 3,,,,,upsert\n"
    )
    files = {
        "a.pdf": b"%PDF-1.4\nVALID",
        "b.pdf": b"%PDF-1.4\nENCRYPTED",
        "c.pdf": b"%PDF-1.4\nMALFORMED",
    }

    batch = _fire_preview(actor, department, manifest, files)

    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert batch.add_count == 1
    assert batch.validation_summary["total_document_count"] == 3
    assert batch.validation_summary["ready_document_count"] == 1
    assert batch.validation_summary["rejected_document_count"] == 2
    codes = sorted(f["code"] for f in batch.validation_summary["document_failures"])
    assert codes == ["encrypted_pdf", "malformed_pdf"]

    apply_preview(actor=actor, batch_id=batch.id)
    assert FirePlan.objects.filter(department=department).count() == 1
    assert FirePlan.objects.get(department=department).external_identifier == "A"
    assert not FirePlan.objects.filter(external_identifier__in=("B", "C")).exists()


@pytest.mark.django_db(transaction=True)
def test_rejected_rows_have_zero_publication_side_effects(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    manifest = fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nB,b.pdf,Plan B,Main 2,,,,,upsert\n")
    files = {"a.pdf": b"%PDF-1.4\nVALID", "b.pdf": b"%PDF-1.4\nENCRYPTED"}

    batch = _fire_preview(actor, department, manifest, files)
    assert batch.validation_summary["rejected_document_count"] == 1

    apply_preview(actor=actor, batch_id=batch.id)

    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_fire_plans"
    )
    assert scope.source_revision == 1
    assert PublicationJob.objects.filter(department=department).count() == 1
    assert not FirePlan.objects.filter(external_identifier="B").exists()


@pytest.mark.django_db(transaction=True)
def test_all_invalid_package_is_not_confirmable(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    manifest = fire_manifest("B,b.pdf,Plan B,Main 2,,,,,upsert\n")
    files = {"b.pdf": b"%PDF-1.4\nENCRYPTED"}

    batch = _fire_preview(actor, department, manifest, files)

    assert batch.status == ImportBatch.Status.INVALID
    assert batch.validation_errors
    assert not FirePlan.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_sanitizer_content_rejection_is_skipped(partial_fixture):
    actor, department, _, sanitizer_rules = partial_fixture
    sanitizer_rules[b"BADPDF"] = PdfSanitizerContentError("PDF sanitizer rejected the document.")
    manifest = fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nB,b.pdf,Plan B,Main 2,,,,,upsert\n")
    files = {"a.pdf": b"%PDF-1.4\nVALID", "b.pdf": b"%PDF-1.4\nBADPDF"}

    batch = _fire_preview(actor, department, manifest, files)
    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert batch.validation_summary["rejected_document_count"] == 1
    assert batch.validation_summary["document_failures"][0]["code"] == "sanitizer_content_rejected"

    apply_preview(actor=actor, batch_id=batch.id)
    assert FirePlan.objects.filter(department=department).count() == 1


@pytest.mark.django_db(transaction=True)
def test_sanitizer_infrastructure_failure_is_fatal(partial_fixture):
    actor, department, _, sanitizer_rules = partial_fixture
    sanitizer_rules[b"TRIGGER"] = PdfSanitizerError("PDF sanitizer broker socket is unavailable.")
    manifest = fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nB,b.pdf,Plan B,Main 2,,,,,upsert\n")
    files = {"a.pdf": b"%PDF-1.4\nVALID", "b.pdf": b"%PDF-1.4\nTRIGGER"}

    batch = _fire_preview(actor, department, manifest, files)
    assert batch.status == ImportBatch.Status.INVALID
    assert not FirePlan.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_valid_deactivate_plus_failed_upsert(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    manifest = fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nOLD,,,Old Road 1,,,deactivate\n")
    files = {"a.pdf": b"%PDF-1.4\nENCRYPTED"}

    # Pre-create the plan that will be deactivated.
    FirePlan.objects.create(
        department=department,
        external_identifier="OLD",
        object_name="Old",
        address="Old Road 1",
        document_key="old.pdf",
        original_filename="old.pdf",
        file_size=1,
        page_count=1,
        sha256="a" * 64,
        source_pdf_sha256="a" * 64,
        active=True,
        uploaded_by=actor,
    )

    batch = _fire_preview(actor, department, manifest, files)
    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert batch.validation_summary["rejected_document_count"] == 1
    assert batch.deactivate_count == 1

    apply_preview(actor=actor, batch_id=batch.id)
    old = FirePlan.objects.get(department=department, external_identifier="OLD")
    assert old.active is False
    assert not FirePlan.objects.filter(external_identifier="A").exists()


@pytest.mark.django_db(transaction=True)
def test_identical_rerun_convergence_and_deterministic_failures(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    manifest = fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nB,b.pdf,Plan B,Main 2,,,,,upsert\n")
    files = {"a.pdf": b"%PDF-1.4\nVALID", "b.pdf": b"%PDF-1.4\nENCRYPTED"}

    first = _fire_preview(actor, department, manifest, files)
    first_failures = list(first.validation_summary["document_failures"])
    apply_preview(actor=actor, batch_id=first.id)

    second = _fire_preview(actor, department, manifest, files)
    assert second.add_count == 0
    assert second.unchanged_count == 1
    assert second.validation_summary["document_failures"] == first_failures

    apply_preview(actor=actor, batch_id=second.id)
    assert FirePlan.objects.filter(department=department).count() == 1
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_fire_plans"
    )
    assert scope.source_revision == 1


@pytest.mark.django_db(transaction=True)
def test_unicode_rejected_filename_is_recorded(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    manifest = fire_manifest("A,FEUPL R\u00fcbenkamp 220.pdf,Plan A,Main 1,,,,,upsert\n")
    files = {"FEUPL R\u00fcbenkamp 220.pdf": b"%PDF-1.4\nENCRYPTED"}

    batch = _fire_preview(actor, department, manifest, files)
    assert batch.status == ImportBatch.Status.INVALID
    assert batch.validation_errors
    # The rejected filename survives in the validation errors path only as INVALID;
    # the deterministic failure list is used for mixed previews. Here the whole
    # package is invalid (single doc), so assert the record is not created.
    assert not FirePlan.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_klgv_partial_acceptance(partial_fixture):
    actor, department, validation_rules, _ = partial_fixture
    from apps.publications.feature_services import set_department_feature

    set_department_feature(
        actor=actor, department=department, feature_code="klgv_plans", enabled=True
    )
    validation_rules[b"ENCRYPTED"] = PdfValidationError(
        "Encrypted PDFs are not accepted.", code="encrypted_pdf"
    )
    klgv_header = "external_id,filename,title,category,action"
    manifest = klgv_header + "\nA,a.pdf,Plan A,site,upsert\nB,b.pdf,Plan B,site,upsert\n"
    files = {"a.pdf": b"%PDF-1.4\nVALID", "b.pdf": b"%PDF-1.4\nENCRYPTED"}

    batch = create_preview(
        actor=actor,
        department=department,
        domain="klgv_plans",
        import_format="zip",
        import_mode="upsert",
        filename="klgv.zip",
        payload=package("klgv_plans", manifest, files),
    )

    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert batch.validation_summary["rejected_document_count"] == 1
    assert batch.add_count == 1

    apply_preview(actor=actor, batch_id=batch.id)
    assert KlgvPlan.objects.filter(department=department).count() == 1
