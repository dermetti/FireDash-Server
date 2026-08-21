"""Bounded, server-side-filtered Hydrant list."""

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.reference_data.models import Hydrant


def _hydrants(department, specs):
    Hydrant.objects.bulk_create(
        [
            Hydrant(
                department=department,
                external_identifier=external_identifier,
                location=Point(10.0, 53.0, srid=4326),
                hydrant_type=hydrant_type,
                diameter_mm=diameter_mm,
                status=status,
            )
            for external_identifier, hydrant_type, diameter_mm, status in specs
        ]
    )


@pytest.fixture
def hydrant_list_context(db):
    actor = User.objects.create_user("hydrant-list@example.test", "List", "safe-password")
    department = Department.objects.create(name="List", short_code="LST", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    return actor, department


def _url(department):
    return reverse("reference-data-hydrants", args=(department.id,))


@pytest.mark.django_db
def test_default_page_caps_at_100(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(department, [(f"H-{i:04d}", "underground", 100, "ACTIVE") for i in range(250)])
    client.force_login(actor)

    response = client.get(_url(department))

    assert response.status_code == 200
    assert len(response.context["hydrants"]) == 100
    assert response.context["total_count"] == 250
    assert response.context["page"].number == 1
    assert response.context["page"].has_next()


@pytest.mark.django_db
def test_identifier_search_is_server_side(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(
        department,
        [(f"H-{i:04d}", "underground", 100, "ACTIVE") for i in range(50)]
        + [(f"X-{i:04d}", "underground", 100, "ACTIVE") for i in range(50)],
    )
    client.force_login(actor)

    response = client.get(_url(department), {"q": "H-0"})

    identifiers = [h.external_identifier for h in response.context["hydrants"]]
    assert identifiers
    assert all(identifier.startswith("H-") for identifier in identifiers)
    assert response.context["total_count"] == 50


@pytest.mark.django_db
def test_status_filter(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(
        department,
        [(f"A-{i:04d}", "underground", 100, "ACTIVE") for i in range(40)]
        + [(f"I-{i:04d}", "underground", 100, "INACTIVE") for i in range(30)],
    )
    client.force_login(actor)

    response = client.get(_url(department), {"status": "INACTIVE"})

    assert response.context["total_count"] == 30
    assert all(h.status == "INACTIVE" for h in response.context["hydrants"])


@pytest.mark.django_db
def test_hydrant_type_filter(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(
        department,
        [(f"U-{i:04d}", "underground", 100, "ACTIVE") for i in range(25)]
        + [(f"W-{i:04d}", "wall", 100, "ACTIVE") for i in range(25)],
    )
    client.force_login(actor)

    response = client.get(_url(department), {"hydrant_type": "wall"})

    assert response.context["total_count"] == 25
    assert all(h.hydrant_type == "wall" for h in response.context["hydrants"])


@pytest.mark.django_db
def test_diameter_filter(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(
        department,
        [(f"D-{i:04d}", "underground", 100, "ACTIVE") for i in range(20)]
        + [(f"E-{i:04d}", "underground", 150, "ACTIVE") for i in range(20)],
    )
    client.force_login(actor)

    response = client.get(_url(department), {"diameter_mm": "150"})

    assert response.context["total_count"] == 20
    assert all(h.diameter_mm == 150 for h in response.context["hydrants"])


@pytest.mark.django_db
def test_filters_never_expose_another_department(client, hydrant_list_context):
    actor, department = hydrant_list_context
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    _hydrants(department, [(f"H-{i:04d}", "underground", 100, "ACTIVE") for i in range(30)])
    _hydrants(other, [(f"O-{i:04d}", "underground", 100, "ACTIVE") for i in range(30)])
    client.force_login(actor)

    response = client.get(_url(department))

    identifiers = [h.external_identifier for h in response.context["hydrants"]]
    assert identifiers
    assert all(not identifier.startswith("O-") for identifier in identifiers)
    assert response.context["total_count"] == 30


@pytest.mark.django_db
def test_pagination_preserves_filters(client, hydrant_list_context):
    actor, department = hydrant_list_context
    _hydrants(
        department,
        [(f"A-{i:04d}", "underground", 100, "ACTIVE") for i in range(150)]
        + [(f"I-{i:04d}", "underground", 100, "INACTIVE") for i in range(50)],
    )
    client.force_login(actor)

    response = client.get(_url(department), {"status": "ACTIVE", "page": "2"})

    assert response.context["page"].number == 2
    assert response.context["total_count"] == 150
    assert "status=ACTIVE" in response.context["page_query"]
    assert all(h.status == "ACTIVE" for h in response.context["hydrants"])


@pytest.mark.django_db
def test_ordering_is_deterministic(client, hydrant_list_context):
    actor, department = hydrant_list_context
    identifiers = ["H-0003", "H-0001", "H-0002", "H-0000"]
    _hydrants(
        department, [(identifier, "underground", 100, "ACTIVE") for identifier in identifiers]
    )
    client.force_login(actor)

    response = client.get(_url(department))

    assert [h.external_identifier for h in response.context["hydrants"]] == sorted(identifiers)
