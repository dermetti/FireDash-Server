"""Permanent canonical geometry and descriptive hydrant location."""

import pytest
from django.contrib.gis.geos import Point
from django.db import DatabaseError, transaction

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.builders import build_source_payload
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import Hydrant, KlgvPlan


@pytest.mark.django_db
def test_baseline_hydrant_geometry_and_descriptive_location_are_independent():
    user = User.objects.create_user("geometry@example.test", "Geometry", "password")
    department = Department.objects.create(name="Canonical", short_code="CAN", created_by=user)
    point = Point(8.682127, 50.110924, srid=4326)
    hydrant = Hydrant.objects.create(
        department=department, external_identifier="H-1", geometry=point, status="ACTIVE"
    )
    hydrant.refresh_from_db()
    assert hydrant.location is None and hydrant.geometry.ewkb == point.ewkb
    hydrant.location = "Beside the gate"
    hydrant.save(update_fields=["location"])
    payload = build_source_payload(
        definition=get_dataset_definition("department_hydrants"),
        department=department,
        station=None,
    )
    assert payload["features"][0]["geometry"] == {
        "type": "Point",
        "coordinates": [8.682127, 50.110924],
    }
    with pytest.raises(DatabaseError), transaction.atomic():
        Hydrant.objects.create(department=department, external_identifier="H-1", geometry=point)
    plan = KlgvPlan.objects.create(
        department=department,
        external_identifier="K-1",
        object_name="Hall",
        address="Street 1",
        postal_code="12345",
        city="Town",
        location=point,
        path="test/canonical.pdf",
        original_filename="canonical.pdf",
        file_size=12,
        page_count=1,
        sha256="a" * 64,
        source_pdf_sha256="b" * 64,
        uploaded_by=user,
    )
    plan.refresh_from_db()
    assert plan.location is not None
    assert plan.location.ewkb == point.ewkb
    assert (plan.object_name, plan.address, plan.postal_code, plan.city) == (
        "Hall",
        "Street 1",
        "12345",
        "Town",
    )
