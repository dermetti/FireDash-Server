"""Canonical source-fingerprint regressions for publication dirty detection."""

import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload, source_fingerprint
from apps.publications.diffs import source_diff
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan, Hydrant


@pytest.fixture
def fingerprint_context(db):
    admin = User.objects.create_user("fingerprint@example.test", "Fingerprint", "safe-password")
    department = Department.objects.create(name="Fingerprints", short_code="FPR", created_by=admin)
    return admin, department


@pytest.mark.django_db
def test_hydrant_fingerprint_uses_published_content_not_internal_metadata(fingerprint_context):
    _, department = fingerprint_context
    definition = get_dataset_definition("department_hydrants")
    hydrant = Hydrant.objects.create(
        department=department,
        external_identifier="FB-002",
        geometry=Point(10.0, 53.0, srid=4326),
        street="Station road",
        location="Fahrbahn",
        source_metadata={"import_batch": "internal"},
    )

    original = source_fingerprint(definition=definition, department=department, station=None)
    hydrant.flow_information = "internal maintenance note"
    hydrant.source_metadata = {"import_batch": "changed"}
    hydrant.save(update_fields=("flow_information", "source_metadata"))
    assert (
        source_fingerprint(definition=definition, department=department, station=None) == original
    )

    hydrant.street = "New station road"
    hydrant.save(update_fields=("street",))
    assert (
        source_fingerprint(definition=definition, department=department, station=None) != original
    )

    original = source_fingerprint(definition=definition, department=department, station=None)
    hydrant.location = "Fußweg"
    hydrant.save(update_fields=("location",))
    assert (
        source_fingerprint(definition=definition, department=department, station=None) != original
    )

    payload = build_source_payload(definition=definition, department=department, station=None)
    assert payload["features"][0]["properties"]["location"] == "Fußweg"

    hydrant.location = ""
    hydrant.save(update_fields=("location",))
    payload = build_source_payload(definition=definition, department=department, station=None)
    assert "location" in payload["features"][0]["properties"]
    assert payload["features"][0]["properties"]["location"] is None


@pytest.mark.django_db
def test_document_source_manifest_uses_stable_sanitized_pdf_hash_and_logical_metadata(
    fingerprint_context,
):
    admin, department = fingerprint_context
    definition = get_dataset_definition("department_fire_plans")
    plan = FirePlan.objects.create(
        department=department,
        external_identifier="SITE-1",
        object_name="Station plan",
        address="Plan street 1",
        postal_code="20095",
        city="Hamburg",
        document_key="plan-1.pdf",
        original_filename="original.pdf",
        file_size=100,
        page_count=2,
        sha256="a" * 64,
        source_pdf_sha256="b" * 64,
        uploaded_by=admin,
    )

    payload = build_source_payload(definition=definition, department=department, station=None)
    original = source_fingerprint(definition=definition, department=department, station=None)
    assert payload["fire_plans"][0]["sha256"] == "a" * 64
    assert payload["fire_plans"][0]["path"] == f"plans/{plan.id}.pdf"

    plan.source_pdf_sha256 = "c" * 64
    plan.save(update_fields=("source_pdf_sha256",))
    assert (
        source_fingerprint(definition=definition, department=department, station=None) == original
    )

    plan.sha256 = "d" * 64
    plan.save(update_fields=("sha256",))
    assert (
        source_fingerprint(definition=definition, department=department, station=None) != original
    )


def test_source_diff_uses_the_canonical_payload_and_hides_pdf_hash_values():
    before = {
        "fire_plans": [
            {
                "id": "plan-1",
                "object_name": "Station plan",
                "address": "Old street 1",
                "sha256": "a" * 64,
                "path": "plans/plan-1.pdf",
            }
        ]
    }
    after = {
        "fire_plans": [
            {
                "id": "plan-1",
                "object_name": "Station plan",
                "address": "New street 1",
                "sha256": "b" * 64,
                "path": "plans/plan-1.pdf",
            },
            {
                "id": "plan-2",
                "object_name": "New plan",
                "sha256": "c" * 64,
                "path": "plans/plan-2.pdf",
            },
        ]
    }

    diff = source_diff(before, after)

    assert (diff["added"], diff["removed"], diff["changed"]) == (1, 0, 1)
    changed = next(item for item in diff["preview"] if item["kind"] == "Changed")
    assert {field["field"] for field in changed["fields"]} == {"Address", "PDF content"}
    assert "a" * 64 not in str(diff)
    assert "b" * 64 not in str(diff)


def test_source_diff_is_deterministic_and_bounds_its_preview():
    before = {"people": []}
    after = {
        "people": [
            {"id": f"person-{number:02d}", "display_name": f"Person {number:02d}"}
            for number in reversed(range(30))
        ]
    }

    diff = source_diff(before, after)

    assert diff["added"] == 30
    assert diff["truncated"] is True
    assert len(diff["preview"]) == 25
    assert [item["label"] for item in diff["preview"]] == [
        f"Person {number:02d}" for number in range(25)
    ]
