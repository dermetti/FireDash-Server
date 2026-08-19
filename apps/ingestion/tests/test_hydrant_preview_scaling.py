"""Single-Hydrant preview must scale O(imported identifiers), not O(department)."""

import json

import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    create_preview,
    create_single_preview,
)
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.reference_data.models import Hydrant

POPULATION = 3_000


def _geojson(identifier, **changes):
    row = {
        "external_identifier": identifier,
        "longitude": 10.0,
        "latitude": 53.0,
        "hydrant_type": "underground",
        "diameter_mm": 100,
        "status": "ACTIVE",
    } | changes
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [row.pop("longitude"), row.pop("latitude")],
                    },
                    "properties": row,
                }
            ],
        }
    ).encode()


@pytest.fixture
def hydrant_scaling_context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    actor = User.objects.create_user("hydrant-scaling@example.test", "Scaling", "safe-password")
    department = Department.objects.create(name="Scaling", short_code="SCL", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)

    Hydrant.objects.bulk_create(
        [
            Hydrant(
                department=department,
                external_identifier=f"H-{i:05d}",
                location=Point(10.0, 53.0, srid=4326),
                hydrant_type="underground",
                diameter_mm=100,
                status="ACTIVE",
            )
            for i in range(POPULATION)
        ]
    )
    return actor, department


def _single_preview(actor, department, identifier, **changes):
    return create_single_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        values={
            "external_identifier": identifier,
            "longitude": 10.0,
            "latitude": 53.0,
            "hydrant_type": "underground",
            "diameter_mm": 100,
            "status": "ACTIVE",
        }
        | changes,
        original_filename="manual-hydrant-v1.csv",
    )


@pytest.mark.django_db(transaction=True)
def test_single_preview_baseline_is_scoped_to_imported_identifier(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001", diameter_mm=125)

    # The baseline must contain only the affected identifier, never the whole
    # department population.
    assert list(batch.baseline) == ["H-00001"]
    assert batch.update_count == 1
    assert batch.validation_summary["updates"][0]["external_identifier"] == "H-00001"


@pytest.mark.django_db(transaction=True)
def test_single_preview_query_count_is_bounded(
    hydrant_scaling_context, django_assert_max_num_queries
):
    actor, department = hydrant_scaling_context

    with django_assert_max_num_queries(25):
        batch = _single_preview(actor, department, "H-00001", diameter_mm=125)

    assert batch.update_count == 1


@pytest.mark.django_db(transaction=True)
def test_unchanged_row_remains_unchanged(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001")

    assert (batch.add_count, batch.update_count, batch.unchanged_count) == (0, 0, 1)


@pytest.mark.django_db(transaction=True)
def test_new_row_remains_add(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-NEW")

    assert (batch.add_count, batch.update_count, batch.unchanged_count) == (1, 0, 0)
    assert batch.baseline == {}


@pytest.mark.django_db(transaction=True)
def test_explicit_inactive_is_update_not_deactivate(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001", status="INACTIVE")

    assert (batch.add_count, batch.update_count, batch.deactivate_count) == (0, 1, 0)


@pytest.mark.django_db(transaction=True)
def test_stale_canonical_mutation_after_preview_is_rejected(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001", diameter_mm=125)

    hydrant = Hydrant.objects.get(department=department, external_identifier="H-00001")
    hydrant.status = "INACTIVE"
    hydrant.save(update_fields=("status", "updated_at"))

    with pytest.raises(ImportError, match="re-preview"):
        apply_preview(actor=actor, batch_id=batch.id)


@pytest.mark.django_db(transaction=True)
def test_unrelated_hydrant_change_does_not_invalidate_preview(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001", diameter_mm=125)

    # A mutation to a hydrant this import does not touch leaves the import's diff
    # valid, so the preview is not stale and the import applies normally.
    unrelated = Hydrant.objects.get(department=department, external_identifier="H-02999")
    unrelated.status = "INACTIVE"
    unrelated.save(update_fields=("status", "updated_at"))

    apply_preview(actor=actor, batch_id=batch.id)

    assert Hydrant.objects.get(external_identifier="H-00001").diameter_mm == 125


@pytest.mark.django_db(transaction=True)
def test_preview_creates_no_publication_dirty_scope(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    _single_preview(actor, department, "H-00001", diameter_mm=125)

    assert not DatasetScopeState.objects.filter(
        department=department, dataset_type_code="department_hydrants"
    ).exists()
    assert not PublicationJob.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_confirmed_change_marks_correct_scope_dirty(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    batch = _single_preview(actor, department, "H-00001", diameter_mm=125)
    apply_preview(actor=actor, batch_id=batch.id)

    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    assert scope.source_revision == 1
    assert PublicationJob.objects.filter(department=department).count() == 1


@pytest.mark.django_db(transaction=True)
def test_identical_reimport_converges_with_population(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    first = _single_preview(actor, department, "H-00001", diameter_mm=125)
    apply_preview(actor=actor, batch_id=first.id)
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    revision = scope.source_revision

    second = _single_preview(actor, department, "H-00001", diameter_mm=125)
    assert (second.add_count, second.update_count, second.unchanged_count) == (0, 0, 1)
    apply_preview(actor=actor, batch_id=second.id)
    scope.refresh_from_db()
    assert scope.source_revision == revision


@pytest.mark.django_db(transaction=True)
def test_large_batch_preview_loads_only_relevant_identifiers(hydrant_scaling_context):
    actor, department = hydrant_scaling_context
    # A small batch only touches the identifiers it names, never the whole table.
    batch = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="geojson",
        import_mode="merge",
        filename="hydrants.geojson",
        payload=_geojson("H-00001", diameter_mm=125),
    )

    assert list(batch.baseline) == ["H-00001"]
    assert batch.update_count == 1
