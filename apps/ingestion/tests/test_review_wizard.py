"""Document-update review wizard state machine (DB-backed)."""

import hashlib
import io
import shutil
import zipfile

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.pdf_packages import manifest_member_name
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    approve_all_review_decisions,
    create_preview,
    set_review_decision,
)
from apps.organizations.models import Department
from apps.publications.models import PublicationJob
from apps.reference_data.models import FirePlan

FIRE_HEADER = (
    "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,"
    "fsd_location,bmz_location,rwa_info,action"
)

OLD_CONTENT = b"%PDF-1.4\nOLD"
NEW_CONTENT = b"%PDF-1.4\nNEW"


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
def wizard_fixture(db, settings, tmp_path, monkeypatch):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    quarantine_root = tmp_path / "quarantine"
    output_root = tmp_path / "sanitized"
    accepted_root = tmp_path / "accepted"
    settings.REFERENCE_DATA_QUARANTINE_ROOT = quarantine_root
    settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT = output_root
    settings.REFERENCE_DATA_ACCEPTED_ROOT = accepted_root

    actor = User.objects.create_user("wizard@example.test", "Wizard", "safe-password")
    department = Department.objects.create(name="Wizard", short_code="WIZ", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)

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
        shutil.copyfile(quarantined_input, sanitized_output)

    def validate(path, **kwargs):
        content = path.read_bytes()
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

    return actor, department


def _existing_plan(department, actor, external_identifier, content):
    digest = hashlib.sha256(content).hexdigest()
    return FirePlan.objects.create(
        department=department,
        external_identifier=external_identifier,
        object_name="Old",
        address="Main 1",
        document_key=f"{external_identifier}.pdf",
        original_filename=f"{external_identifier}.pdf",
        file_size=len(content),
        page_count=1,
        sha256=digest,
        source_pdf_sha256=digest,
        active=True,
        uploaded_by=actor,
    )


def _update_preview(actor, department):
    return create_preview(
        actor=actor,
        department=department,
        domain="fire_plans",
        import_format="zip",
        import_mode="upsert",
        filename="plans.zip",
        payload=package(
            "fire_plans",
            fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\n"),
            {"a.pdf": NEW_CONTENT},
        ),
    )


def _coordinate_update_preview(actor, department, *external_identifiers):
    rows = "\n".join(
        f"{external_identifier},{external_identifier.lower()}.pdf,Plan {external_identifier},"
        f"Main {external_identifier},,,,,upsert"
        for external_identifier in external_identifiers
    )
    return create_preview(
        actor=actor,
        department=department,
        domain="fire_plans",
        import_format="zip",
        import_mode="upsert",
        filename="plans.zip",
        payload=package(
            "fire_plans",
            fire_manifest(rows),
            {
                f"{external_identifier.lower()}.pdf": NEW_CONTENT
                for external_identifier in external_identifiers
            },
        ),
    )


@pytest.mark.django_db(transaction=True)
def test_pending_update_blocks_confirm(wizard_fixture):
    actor, department = wizard_fixture
    _existing_plan(department, actor, "A", OLD_CONTENT)

    batch = _update_preview(actor, department)
    assert batch.update_count == 1
    assert batch.validation_summary["review_items"][0]["key"]
    assert batch.validation_summary["review_decisions"] == {}

    with pytest.raises(ImportError, match="Pending update review"):
        apply_preview(actor=actor, batch_id=batch.id)

    plan = FirePlan.objects.get(department=department, external_identifier="A")
    assert plan.sha256 == hashlib.sha256(OLD_CONTENT).hexdigest()


@pytest.mark.django_db(transaction=True)
def test_approved_update_is_applied(wizard_fixture):
    actor, department = wizard_fixture
    _existing_plan(department, actor, "A", OLD_CONTENT)

    batch = _update_preview(actor, department)
    key = batch.validation_summary["review_items"][0]["key"]
    set_review_decision(actor=actor, batch_id=batch.id, key=key, decision="approved")

    apply_preview(actor=actor, batch_id=batch.id)

    plan = FirePlan.objects.get(department=department, external_identifier="A")
    assert plan.sha256 == hashlib.sha256(NEW_CONTENT).hexdigest()


@pytest.mark.django_db(transaction=True)
def test_skipped_update_has_zero_effect(wizard_fixture):
    actor, department = wizard_fixture
    _existing_plan(department, actor, "A", OLD_CONTENT)

    batch = _update_preview(actor, department)
    key = batch.validation_summary["review_items"][0]["key"]
    set_review_decision(actor=actor, batch_id=batch.id, key=key, decision="skipped")

    applied = apply_preview(actor=actor, batch_id=batch.id)

    plan = FirePlan.objects.get(department=department, external_identifier="A")
    assert plan.sha256 == hashlib.sha256(OLD_CONTENT).hexdigest()
    assert applied.validation_summary["skipped_update_count"] == 1
    assert not PublicationJob.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_approve_all_pending_updates(wizard_fixture):
    actor, department = wizard_fixture
    _existing_plan(department, actor, "A", OLD_CONTENT)
    _existing_plan(department, actor, "B", OLD_CONTENT)

    batch = create_preview(
        actor=actor,
        department=department,
        domain="fire_plans",
        import_format="zip",
        import_mode="upsert",
        filename="plans.zip",
        payload=package(
            "fire_plans",
            fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\nB,b.pdf,Plan B,Main 2,,,,,upsert\n"),
            {"a.pdf": NEW_CONTENT, "b.pdf": NEW_CONTENT},
        ),
    )
    assert batch.update_count == 2

    approve_all_review_decisions(actor=actor, batch_id=batch.id)

    apply_preview(actor=actor, batch_id=batch.id)

    new_digest = hashlib.sha256(NEW_CONTENT).hexdigest()
    assert FirePlan.objects.get(external_identifier="A").sha256 == new_digest
    assert FirePlan.objects.get(external_identifier="B").sha256 == new_digest


@pytest.mark.django_db(transaction=True)
def test_review_decision_is_bound_to_preview_state(wizard_fixture):
    actor, department = wizard_fixture
    plan = _existing_plan(department, actor, "A", OLD_CONTENT)

    batch = _update_preview(actor, department)
    key = batch.validation_summary["review_items"][0]["key"]
    set_review_decision(actor=actor, batch_id=batch.id, key=key, decision="approved")

    # A canonical mutation after review makes the preview stale.
    plan.sha256 = hashlib.sha256(b"tampered").hexdigest()
    plan.save(update_fields=("sha256",))

    with pytest.raises(ImportError, match="Canonical documents changed"):
        apply_preview(actor=actor, batch_id=batch.id)


@pytest.mark.django_db(transaction=True)
def test_adds_do_not_require_review(wizard_fixture):
    actor, department = wizard_fixture

    batch = create_preview(
        actor=actor,
        department=department,
        domain="fire_plans",
        import_format="zip",
        import_mode="upsert",
        filename="plans.zip",
        payload=package(
            "fire_plans",
            fire_manifest("A,a.pdf,Plan A,Main 1,,,,,upsert\n"),
            {"a.pdf": NEW_CONTENT},
        ),
    )
    assert batch.add_count == 1
    assert batch.update_count == 0
    assert batch.validation_summary["review_items"] == []

    apply_preview(actor=actor, batch_id=batch.id)

    assert FirePlan.objects.filter(department=department).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("field", "invalid_value", "other_value", "expected_error"),
    (
        ("longitude", "181", "53.551323", "less than or equal to 180"),
        ("longitude", "-181", "53.551323", "greater than or equal to -180"),
        ("longitude", "not-a-number", "53.551323", "Enter a number"),
        ("latitude", "91", "10.000992", "less than or equal to 90"),
        ("latitude", "-91", "10.000992", "greater than or equal to -90"),
        ("latitude", "not-a-number", "10.000992", "Enter a number"),
    ),
)
def test_invalid_coordinate_accept_returns_bound_htmx_review_region(
    client, wizard_fixture, field, invalid_value, other_value, expected_error
):
    actor, department = wizard_fixture
    plan = _existing_plan(department, actor, "A", OLD_CONTENT)
    batch = _coordinate_update_preview(actor, department, "A")
    key = batch.validation_summary["review_items"][0]["key"]

    client.force_login(actor)
    payload = {"longitude": other_value, "latitude": other_value}
    payload[field] = invalid_value
    response = client.post(
        reverse("ingestion-review-approve", args=(department.id, batch.id, key)),
        payload,
        HTTP_HX_REQUEST="true",
    )

    content = response.content.decode()
    batch.refresh_from_db()
    plan.refresh_from_db()
    assert response.status_code == 200
    assert '<div id="import-review-region">' in content
    assert f'value="{invalid_value}"' in content
    assert expected_error in content
    assert key not in batch.validation_summary["review_decisions"]
    assert plan.sha256 == hashlib.sha256(OLD_CONTENT).hexdigest()
    assert plan.location is None


@pytest.mark.django_db(transaction=True)
def test_valid_coordinate_accept_stages_value_and_advances_without_canonical_mutation(
    client, wizard_fixture
):
    actor, department = wizard_fixture
    first = _existing_plan(department, actor, "A", OLD_CONTENT)
    second = _existing_plan(department, actor, "B", OLD_CONTENT)
    batch = _coordinate_update_preview(actor, department, "A", "B")
    key = batch.validation_summary["review_items"][0]["key"]

    client.force_login(actor)
    response = client.post(
        reverse("ingestion-review-approve", args=(department.id, batch.id, key)),
        {"longitude": "10.000992", "latitude": "53.551323"},
        HTTP_HX_REQUEST="true",
    )

    content = response.content.decode()
    batch.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    assert response.status_code == 200
    assert '<div id="import-review-region">' in content
    assert "B" in content
    assert batch.normalized_intent["rows"][0]["longitude"] == pytest.approx(10.000992)
    assert batch.normalized_intent["rows"][0]["latitude"] == pytest.approx(53.551323)
    assert batch.validation_summary["review_decisions"][key]["decision"] == "approved"
    assert first.sha256 == hashlib.sha256(OLD_CONTENT).hexdigest()
    assert first.location is None
    assert second.sha256 == hashlib.sha256(OLD_CONTENT).hexdigest()


@pytest.mark.django_db(transaction=True)
def test_coordinate_accept_preserves_existing_partial_coordinate(client, wizard_fixture):
    actor, department = wizard_fixture
    _existing_plan(department, actor, "A", OLD_CONTENT)
    batch = _coordinate_update_preview(actor, department, "A")
    row = batch.normalized_intent["rows"][0]
    row["latitude"] = 53.551323
    batch.normalized_intent = {"rows": [row]}
    batch.save(update_fields=("normalized_intent",))
    key = batch.validation_summary["review_items"][0]["key"]

    client.force_login(actor)
    response = client.post(
        reverse("ingestion-review-approve", args=(department.id, batch.id, key)),
        {"longitude": "10.000992", "latitude": "53.551323"},
        HTTP_HX_REQUEST="true",
    )

    batch.refresh_from_db()
    assert response.status_code == 200
    assert batch.normalized_intent["rows"][0]["longitude"] == pytest.approx(10.000992)
    assert batch.normalized_intent["rows"][0]["latitude"] == pytest.approx(53.551323)
