"""Strict input and nullable-output contract for Hydrant location."""

import json

import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.ingestion.parsers import ImportValidationError, parse_hydrants
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import Hydrant


def _geojson(properties):
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [8.682127, 50.110924]},
                    "properties": properties,
                }
            ],
        }
    ).encode()


def _properties(**changes):
    return {
        "external_identifier": "H-CONTRACT-1",
        "street": "Example Street",
        "house_number": "1",
        "location": "Fahrbahn",
        "hydrant_type": "underground",
        "diameter_mm": 100,
        "status": "ACTIVE",
    } | changes


def test_geojson_location_key_is_required():
    properties = _properties()
    del properties["location"]

    with pytest.raises(ImportValidationError, match="properties do not match schema"):
        parse_hydrants(payload=_geojson(properties), import_format="geojson")


@pytest.mark.parametrize("location", [None, ""])
def test_geojson_location_key_allows_null_and_empty_text(location):
    rows = parse_hydrants(payload=_geojson(_properties(location=location)), import_format="geojson")

    assert rows[0]["location"] == location


@pytest.mark.django_db
@pytest.mark.parametrize("location", [None, ""])
def test_published_geojson_always_emits_nullable_location_key(location):
    user = User.objects.create_user("contract@example.test", "Contract", "safe-password")
    department = Department.objects.create(name="Contract", short_code="CNT", created_by=user)
    Hydrant.objects.create(
        department=department,
        external_identifier="H-CONTRACT-1",
        geometry=Point(8.682127, 50.110924, srid=4326),
        location=location,
        status="ACTIVE",
    )

    payload = build_source_payload(
        definition=get_dataset_definition("department_hydrants"), department=department, station=None
    )
    properties = payload["features"][0]["properties"]
    assert "location" in properties
    assert properties["location"] is None
