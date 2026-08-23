import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("route", "title", "fixed_format"),
    (
        ("ingestion-import-hydrants", "Import Hydrants", None),
        ("ingestion-import-personnel", "Import Personnel", "CSV"),
        ("ingestion-import-fire-plans", "Import Fire Plans", "PDF + CSV ZIP package"),
        ("ingestion-import-klgv-plans", "Import KLGV Plans", "PDF + CSV ZIP package"),
    ),
)
def test_domain_import_pages_have_explicit_human_presentation(
    client, import_page_context, route, title, fixed_format
):
    actor, department, *_ = import_page_context
    client.force_login(actor)
    response = client.get(reverse(route, args=(department.id,)))
    content = response.content.decode()
    assert response.status_code == 200
    assert title in content
    assert "Import and review" in content
    assert "Create preview" not in content
    assert "Fire_Plans" not in content
    if fixed_format:
        assert fixed_format in content
        assert 'name="import_format"' not in content or 'type="hidden"' in content


@pytest.mark.django_db
def test_hydrant_import_page_offers_only_csv_and_geojson_and_scopes_recent_batches(
    client, import_page_context
):
    actor, department, _, other, _ = import_page_context
    ImportBatch.objects.create(
        department=department,
        actor=actor,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        original_filename="hydrants.csv",
        upload_sha256="a" * 64,
        staging_key="test/hydrants",
    )
    ImportBatch.objects.create(
        department=department,
        actor=actor,
        domain=ImportBatch.Domain.PERSONNEL,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.UPSERT,
        original_filename="personnel.csv",
        upload_sha256="b" * 64,
        staging_key="test/personnel",
    )
    ImportBatch.objects.create(
        department=other,
        actor=actor,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        original_filename="other.csv",
        upload_sha256="c" * 64,
        staging_key="test/other",
    )
    client.force_login(actor)
    response = client.get(reverse("ingestion-import-hydrants", args=(department.id,)))
    content = response.content.decode()
    assert "hydrants.csv" in content
    assert "personnel.csv" not in content
    assert "other.csv" not in content
    assert "GeoJSON" in content and "CSV" in content
    assert ">JSON<" not in content
