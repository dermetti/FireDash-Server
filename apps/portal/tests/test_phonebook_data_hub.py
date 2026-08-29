import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department


@pytest.mark.django_db
def test_phonebook_and_personnel_have_distinct_data_hub_icons(client):
    user = User.objects.create_user("hub-icons@example.test", "Hub", "password")
    department = Department.objects.create(name="Hub", short_code="HUB", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    client.force_login(user)
    response = client.get(reverse("portal-data-hub", args=[department.id]))
    assert response.status_code == 200
    modules = {module["name"]: module["icon"] for module in response.context["modules"]}
    assert modules["Phonebook"] == "phone"
    assert modules["Personnel"] == "people"
