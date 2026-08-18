import pytest
from django.contrib.gis.geos import Point

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.reference_data.models import Hydrant


@pytest.mark.django_db
def test_unicode_canonical_data_and_json_metadata_round_trip():
    actor = User.objects.create_user("müller@example.test", "Müller", "safe-password")
    department = Department.objects.create(
        name="Österreich Straße", short_code="UTF", created_by=actor
    )
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    hydrant = Hydrant.objects.create(
        department=department,
        external_identifier="Straße",
        location=Point(10.0, 53.0, srid=4326),
        source_metadata={"name": "Müller", "place": "Österreich", "street": "Großstraße"},
    )
    hydrant.refresh_from_db()
    assert hydrant.external_identifier == "Straße"
    assert hydrant.source_metadata == {
        "name": "Müller",
        "place": "Österreich",
        "street": "Großstraße",
    }
