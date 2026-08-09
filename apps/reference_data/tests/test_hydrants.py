import json
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.reference_data.hydrants import HydrantImportError, parse_feature_collection
from apps.reference_data.models import Hydrant, HydrantImportPreview
from apps.reference_data.services import confirm_hydrant_preview, create_hydrant_preview


def geojson(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def hydrant_feature(*, coordinates=None, properties=None, geometry_type="Point"):
    if coordinates is None:
        coordinates = [-73.9857, 40.7484]
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": properties or {},
    }


def test_parse_feature_collection_normalizes_valid_properties_and_metadata():
    features = parse_feature_collection(
        geojson(
            hydrant_feature(
                properties={
                    "external_identifier": "  H-101  ",
                    "hydrant_type": "  wet barrel ",
                    "flow_information": "  1,500 GPM  ",
                    "status": " in service ",
                    "source_metadata": {"surveyed": True, "year": 2025, "source": "GIS"},
                }
            )
        )
    )

    assert len(features) == 1
    assert features[0].as_json() == {
        "longitude": -73.9857,
        "latitude": 40.7484,
        "external_identifier": "H-101",
        "hydrant_type": "wet barrel",
        "flow_information": "1,500 GPM",
        "status": "in service",
        "source_metadata": {"surveyed": True, "year": 2025, "source": "GIS"},
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{not json", "valid UTF-8 GeoJSON"),
        (json.dumps({"type": "Feature", "features": []}).encode(), "FeatureCollection"),
        (json.dumps({"type": "FeatureCollection"}).encode(), "features array"),
        (geojson(hydrant_feature(geometry_type="LineString")), "must be a Point"),
        (geojson(hydrant_feature(coordinates=[181, 40])), "coordinates are invalid"),
        (geojson(hydrant_feature(coordinates=[-73])), "must contain longitude and latitude"),
        (geojson(hydrant_feature(properties={"unexpected": "value"})), "unsupported fields"),
        (
            geojson(hydrant_feature(properties={"source_metadata": {"nested": {"value": 1}}})),
            "source metadata is invalid",
        ),
    ],
)
def test_parse_feature_collection_rejects_invalid_geojson(payload, message):
    with pytest.raises(HydrantImportError, match=message):
        parse_feature_collection(payload)


@override_settings(MAX_HYDRANT_IMPORT_FEATURES=1)
def test_parse_feature_collection_enforces_feature_limit():
    with pytest.raises(HydrantImportError, match="feature limit exceeded"):
        parse_feature_collection(geojson(hydrant_feature(), hydrant_feature()))


@pytest.fixture
def reference_data_roles(db):
    department_admin = User.objects.create_user(
        "department-admin@example.test", "Department Admin", "safe-password"
    )
    other_admin = User.objects.create_user(
        "other-admin@example.test", "Other Admin", "safe-password"
    )
    outsider = User.objects.create_user("outsider@example.test", "Outsider", "safe-password")
    department = Department.objects.create(
        name="Reference Data", short_code="REF", created_by=department_admin
    )
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=department_admin
    )
    DepartmentMembership.objects.create(
        user=other_admin, department=department, created_by=department_admin
    )
    return department_admin, other_admin, outsider, department


@pytest.mark.django_db
def test_create_preview_requires_department_admin(reference_data_roles):
    _, _, outsider, department = reference_data_roles

    with pytest.raises(PermissionDenied, match="Department administrator scope is required"):
        create_hydrant_preview(actor=outsider, department=department, raw_geojson=geojson())

    assert not HydrantImportPreview.objects.exists()


@pytest.mark.django_db
def test_preview_persists_normalized_features_and_duplicate_count(reference_data_roles):
    department_admin, _, _, department = reference_data_roles
    payload = geojson(
        hydrant_feature(properties={"hydrant_type": "wet", "status": "active"}),
        hydrant_feature(properties={"hydrant_type": "wet", "status": "active"}),
    )

    preview = create_hydrant_preview(
        actor=department_admin, department=department, raw_geojson=payload
    )

    assert preview.created_by == department_admin
    assert preview.duplicate_count == 1
    assert preview.normalized_features[0]["longitude"] == -73.9857
    assert preview.expires_at > timezone.now()


@pytest.mark.django_db
def test_confirm_preview_is_limited_to_its_creator_and_consumes_preview(reference_data_roles):
    department_admin, other_admin, _, department = reference_data_roles
    preview = create_hydrant_preview(
        actor=department_admin,
        department=department,
        raw_geojson=geojson(
            hydrant_feature(
                properties={
                    "external_identifier": "H-101",
                    "source_metadata": {"source": "GIS"},
                }
            )
        ),
    )

    with pytest.raises(PermissionDenied, match="preview is unavailable"):
        confirm_hydrant_preview(actor=other_admin, department=department, preview_id=preview.id)

    assert HydrantImportPreview.objects.filter(pk=preview.id).exists()
    assert Hydrant.objects.count() == 0

    assert confirm_hydrant_preview(
        actor=department_admin, department=department, preview_id=preview.id
    ) == (1, 0, 0)
    hydrant = Hydrant.objects.get(external_identifier="H-101")
    assert hydrant.department == department
    assert hydrant.location.x == pytest.approx(-73.9857)
    assert hydrant.location.y == pytest.approx(40.7484)
    assert hydrant.source_metadata == {"source": "GIS"}
    assert not HydrantImportPreview.objects.filter(pk=preview.id).exists()


@pytest.mark.django_db
def test_confirm_preview_rejects_expired_preview(reference_data_roles):
    department_admin, _, _, department = reference_data_roles
    preview = create_hydrant_preview(
        actor=department_admin, department=department, raw_geojson=geojson(hydrant_feature())
    )
    preview.expires_at = timezone.now() - timedelta(seconds=1)
    preview.save(update_fields=("expires_at",))

    with pytest.raises(PermissionDenied, match="preview is unavailable"):
        confirm_hydrant_preview(
            actor=department_admin, department=department, preview_id=preview.id
        )

    assert HydrantImportPreview.objects.filter(pk=preview.id).exists()
    assert Hydrant.objects.count() == 0
