import json

import pytest

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.services import ImportError, apply_preview, create_preview
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.reference_data.models import Hydrant


@pytest.fixture
def context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    actor = User.objects.create_user("hydrants@example.test", "Hydrants", "safe-password")
    department = Department.objects.create(name="Hydrants", short_code="HYD", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    return actor, department


def payload(**changes):
    row = {
        "external_identifier": "H-1",
        "longitude": 10.0,
        "latitude": 53.0,
        "street": "Harbor Road",
        "house_number": "1",
        "location": "Fahrbahn",
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


def preview(actor, department, source):
    return create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="geojson",
        import_mode="merge",
        filename="hydrants.geojson",
        payload=source,
    )


@pytest.mark.django_db
def test_exact_hydrant_reimport_is_unchanged_and_confirmation_is_a_noop(context):
    actor, department = context
    first = preview(actor, department, payload())
    assert (first.add_count, first.update_count, first.unchanged_count) == (1, 0, 0)
    apply_preview(actor=actor, batch_id=first.id)
    hydrant = Hydrant.objects.get(department=department, external_identifier="H-1")
    original_updated_at = hydrant.updated_at
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    original_revision = scope.source_revision
    job_count = PublicationJob.objects.filter(department=department).count()

    second = preview(actor, department, payload())
    assert (
        second.add_count,
        second.update_count,
        second.deactivate_count,
        second.unchanged_count,
    ) == (0, 0, 0, 1)
    apply_preview(actor=actor, batch_id=second.id)
    hydrant.refresh_from_db()
    scope.refresh_from_db()
    assert hydrant.updated_at == original_updated_at
    assert scope.source_revision == original_revision
    assert PublicationJob.objects.filter(department=department).count() == job_count


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [{"diameter_mm": 125}, {"longitude": 10.1}, {"location": "Fußweg"}, {"hydrant_type": "wall"}, {"status": "INACTIVE"}],
)
def test_each_hydrant_business_field_change_is_an_update(context, changes):
    actor, department = context
    apply_preview(actor=actor, batch_id=preview(actor, department, payload()).id)
    changed = preview(actor, department, payload(**changes))
    assert (changed.add_count, changed.update_count, changed.unchanged_count) == (0, 1, 0)
    assert changed.validation_summary["updates"][0]["fields"]


@pytest.mark.django_db
@pytest.mark.parametrize("location", ["Fahrbahn", "", None])
def test_geojson_location_is_preserved_in_canonical_hydrant(context, location):
    actor, department = context
    batch = preview(actor, department, payload(location=location))

    apply_preview(actor=actor, batch_id=batch.id)

    hydrant = Hydrant.objects.get(department=department, external_identifier="H-1")
    assert hydrant.location == location


@pytest.mark.django_db
def test_hydrant_preview_remains_stale_after_canonical_change(context):
    actor, department = context
    apply_preview(actor=actor, batch_id=preview(actor, department, payload()).id)
    candidate = preview(actor, department, payload(diameter_mm=125))
    hydrant = Hydrant.objects.get(department=department, external_identifier="H-1")
    hydrant.status = "INACTIVE"
    hydrant.save(update_fields=("status", "updated_at"))
    with pytest.raises(ImportError, match="re-preview"):
        apply_preview(actor=actor, batch_id=candidate.id)
