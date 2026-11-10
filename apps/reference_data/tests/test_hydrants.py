import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.reference_data.models import Hydrant


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
def test_hydrant_forms_create_and_confirm_import_batches(
    client, reference_data_roles, settings, tmp_path
):
    settings.INGESTION_STAGING_ROOT = tmp_path / "private-import-staging"
    department_admin, _, _, department = reference_data_roles
    client.force_login(department_admin)

    response = client.post(
        reverse("reference-data-hydrant-create", args=(department.id,)),
        {
            "external_identifier": " H-42 ",
            "longitude": "-73.9857",
            "latitude": "40.7484",
            "hydrant_type": " Wet barrel ",
            "diameter_mm": "150",
            "status": "INACTIVE",
        },
    )

    assert response.status_code == 302
    from apps.ingestion.models import ImportBatch
    from apps.ingestion.services import apply_preview

    batch = ImportBatch.objects.get(department=department)
    assert not Hydrant.objects.filter(department=department, external_identifier="H-42").exists()
    apply_preview(actor=department_admin, batch_id=batch.id)
    hydrant = Hydrant.objects.get(department=department, external_identifier="H-42")
    assert (hydrant.hydrant_type, hydrant.diameter_mm, hydrant.status, hydrant.active) == (
        "Wet barrel",
        150,
        "INACTIVE",
        False,
    )

    response = client.post(
        reverse("reference-data-hydrant-edit", args=(hydrant.id,)),
        {
            "external_identifier": "H-42",
            "longitude": "-73.9",
            "latitude": "40.7",
            "hydrant_type": "Dry barrel",
            "flow_information": "",
            "diameter_mm": "100",
        },
    )

    assert response.status_code == 302
    hydrant.refresh_from_db()
    assert (
        hydrant.external_identifier,
        hydrant.geometry.x,
        hydrant.geometry.y,
        hydrant.active,
    ) == (
        "H-42",
        pytest.approx(-73.9),
        pytest.approx(40.7),
        False,
    )
    response = client.post(
        reverse("reference-data-hydrant-lifecycle", args=(hydrant.id,)), {"status": "ACTIVE"}
    )
    assert response.status_code == 302
    hydrant.refresh_from_db()
    assert hydrant.active is True
