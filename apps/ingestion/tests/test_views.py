import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station


@pytest.fixture
def import_page_context(db):
    actor = User.objects.create_user("imports@example.test", "Import Admin", "safe-password")
    department = Department.objects.create(name="Import", short_code="IMP", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    active = Station.objects.create(department=department, name="Active", short_code="ACT")
    Station.objects.create(department=department, name="Inactive", short_code="INA", active=False)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    other_station = Station.objects.create(department=other, name="Other", short_code="OTH")
    return actor, department, active, other, other_station


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query",
    [
        {"domain": "hydrants", "import_format": "geojson", "import_mode": "merge"},
        {"domain": "personnel", "import_format": "csv", "import_mode": "upsert"},
        {"domain": "fire_plans", "import_format": "zip", "import_mode": "upsert"},
        {"domain": "klgv_plans", "import_format": "zip", "import_mode": "upsert"},
    ],
)
def test_department_admin_can_open_each_supported_import_entry_page(
    client, import_page_context, query
):
    actor, department, active, _, other_station = import_page_context
    client.force_login(actor)
    response = client.get(reverse("ingestion-imports", args=(department.id,)), query)
    assert response.status_code == 200
    station_field = response.context["form"].fields["station"]
    assert list(station_field.queryset.values_list("id", flat=True)) == [active.id]
    assert other_station.id not in station_field.queryset.values_list("id", flat=True)


@pytest.mark.django_db
def test_import_page_denies_another_department_administrator(client, import_page_context):
    _, department, _, other, _ = import_page_context
    outsider = User.objects.create_user("outsider@example.test", "Outsider", "safe-password")
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=outsider)
    client.force_login(outsider)
    assert client.get(reverse("ingestion-imports", args=(department.id,))).status_code == 403
