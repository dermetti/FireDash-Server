import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department


@pytest.mark.django_db
def test_fire_plan_add_form_exposes_only_the_canonical_identity_fields(client):
    actor = User.objects.create_user("fire-plan-view@example.test", "Fire Plan", "safe-password")
    department = Department.objects.create(name="Fire Plans", short_code="FPL", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    client.force_login(actor)

    response = client.get(reverse("reference-data-fire-plans", args=(department.id,)))

    assert response.status_code == 200
    form = response.context["form"]
    assert set(form.fields) == {
        "document",
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "fsd_location",
        "bmz_location",
        "rwa_info",
    }
    content = response.content.decode()
    assert "Object reference" not in content
    assert content.index("Longitude") < content.index("Latitude")
    assert "Required when no External ID is available" in content
