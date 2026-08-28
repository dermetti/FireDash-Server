import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.organizations.models import Department
from apps.publications.builders import build_artifact
from apps.publications.models import PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import Hydrant


@pytest.fixture
def hydrant_scope(client, db):
    admin = User.objects.create_user("hydrant-admin@example.test", "Admin", "password")
    department = Department.objects.create(name="Hydrants", short_code="HYD", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    hydrant = Hydrant.objects.create(
        department=department,
        external_identifier="H-001",
        location=Point(10.123, 53.456, srid=4326),
        hydrant_type="Underground",
        diameter_mm=100,
        status=Hydrant.Status.ACTIVE,
    )
    other_department = Department.objects.create(name="Other", short_code="OTH", created_by=admin)
    other = Hydrant.objects.create(
        department=other_department,
        external_identifier="OTHER-1",
        location=Point(11, 54, srid=4326),
    )
    client.force_login(admin)
    return admin, department, hydrant, other


def _payload(**overrides):
    values = {
        "external_identifier": "H-001",
        "longitude": "10.123",
        "latitude": "53.456",
        "hydrant_type": "Above ground",
        "flow_information": "1200 l/min",
        "diameter_mm": "150",
    }
    values.update(overrides)
    return values


@pytest.mark.django_db
def test_hydrant_list_is_bounded_active_by_default_and_filterable(client, hydrant_scope):
    _, department, hydrant, other = hydrant_scope
    Hydrant.objects.bulk_create(
        [
            Hydrant(
                department=department,
                external_identifier=f"HX-{index:03}",
                location=Point(10 + index / 1000, 53, srid=4326),
            )
            for index in range(101)
        ]
    )
    inactive = Hydrant.objects.create(
        department=department,
        external_identifier="H-INACTIVE",
        location=Point(12, 53, srid=4326),
        status=Hydrant.Status.INACTIVE,
    )
    response = client.get(reverse("reference-data-hydrants", args=(department.id,)))
    assert response.status_code == 200
    assert len(response.context["hydrants"]) == 100
    assert response.context["total_count"] == 102
    body = response.content.decode()
    assert "table-responsive" not in body and "Page 1 of 2" in body
    assert reverse("reference-data-hydrant-manage", args=(hydrant.id,)) in body
    assert inactive.external_identifier not in body and other.external_identifier not in body

    inactive_response = client.get(
        reverse("reference-data-hydrants", args=(department.id,)), {"status": "INACTIVE"}
    )
    assert list(inactive_response.context["hydrants"]) == [inactive]


@pytest.mark.django_db
def test_hydrant_modal_lifecycle_delete_publication_and_scope(client, hydrant_scope):
    _, department, hydrant, other = hydrant_scope
    detail_url = reverse("reference-data-hydrant-manage", args=(hydrant.id,))
    detail = client.get(detail_url)
    assert detail.status_code == 200
    expected_actions = ("Edit Data", "Delete Data", "Mark inactive")
    assert all(label in detail.content.decode() for label in expected_actions)
    detail_body = detail.content.decode()
    assert "hydrant-action-modal-container" in detail_body
    assert 'data-bs-toggle="modal"' not in detail_body
    assert "htmx:afterSwap" in detail_body

    edit_url = reverse("reference-data-hydrant-edit", args=(hydrant.id,))
    get_edit = client.get(edit_url, HTTP_HX_REQUEST="true")
    assert get_edit.status_code == 200 and hydrant.external_identifier.encode() in get_edit.content
    assert b'<div class="modal fade"' in get_edit.content
    assert b"modal-dialog" in get_edit.content
    assert b'<div class="modal-content"' in get_edit.content
    assert b'hx-target="#hydrant-action-modal-container"' in get_edit.content
    assert b'type="submit"' in get_edit.content
    invalid = client.post(edit_url, _payload(longitude="999", hydrant_type="Entered"))
    assert invalid.status_code == 200 and b"Entered" in invalid.content
    hydrant.refresh_from_db()
    assert hydrant.hydrant_type == "Underground"

    updated = client.post(edit_url, _payload())
    assert updated.status_code == 302
    hydrant.refresh_from_db()
    assert (hydrant.hydrant_type, hydrant.flow_information, hydrant.diameter_mm) == (
        "Above ground",
        "1200 l/min",
        150,
    )
    assert AuditEvent.objects.filter(
        action="reference_data.hydrant_updated", target_uuid=hydrant.id
    ).exists()
    assert PublicationJob.objects.filter(
        department=department, dataset_type_code="department_hydrants"
    ).exists()

    htmx_success = client.post(edit_url, _payload(), HTTP_HX_REQUEST="true")
    assert htmx_success.status_code == 204
    assert htmx_success["HX-Redirect"] == detail_url

    assert client.get(reverse("reference-data-hydrant-edit", args=(other.id,))).status_code == 404
    lifecycle = client.post(
        reverse("reference-data-hydrant-lifecycle", args=(hydrant.id,)), {"status": "INACTIVE"}
    )
    assert lifecycle.status_code == 302
    hydrant.refresh_from_db()
    assert hydrant.active is False
    artifact = build_artifact(
        definition=get_dataset_definition("department_hydrants"),
        department=department,
        station=None,
        source_revision=1,
    )
    assert hydrant.external_identifier.encode() not in artifact

    assert (
        client.post(
            reverse("reference-data-hydrant-lifecycle", args=(hydrant.id,)), {"status": "ACTIVE"}
        ).status_code
        == 302
    )
    delete_url = reverse("reference-data-hydrant-delete", args=(hydrant.id,))
    assert client.get(delete_url, HTTP_HX_REQUEST="true").status_code == 200
    assert Hydrant.objects.filter(pk=hydrant.id).exists()
    assert client.post(delete_url).status_code == 302
    assert not Hydrant.objects.filter(pk=hydrant.id).exists()
    assert AuditEvent.objects.filter(
        action="reference_data.hydrant_deleted", target_uuid=hydrant.id
    ).exists()
    artifact_after_delete = build_artifact(
        definition=get_dataset_definition("department_hydrants"),
        department=department,
        station=None,
        source_revision=2,
    )
    assert hydrant.external_identifier.encode() not in artifact_after_delete


@pytest.mark.django_db
def test_hydrant_mutation_gets_do_not_mutate_and_csrf_remains_enabled(client, hydrant_scope):
    admin, _, hydrant, _ = hydrant_scope
    for url in (
        reverse("reference-data-hydrant-edit", args=(hydrant.id,)),
        reverse("reference-data-hydrant-delete", args=(hydrant.id,)),
    ):
        assert client.get(url).status_code == 200
    assert Hydrant.objects.filter(pk=hydrant.id).exists()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(admin)
    delete_response = csrf_client.post(reverse("reference-data-hydrant-delete", args=(hydrant.id,)))
    assert delete_response.status_code == 403
    assert Hydrant.objects.filter(pk=hydrant.id).exists()


@pytest.mark.django_db
def test_domain_import_entries_lock_domain_formats_and_recent_batches(client, hydrant_scope):
    admin, department, _, _ = hydrant_scope
    ImportBatch.objects.create(
        domain=ImportBatch.Domain.HYDRANTS,
        department=department,
        import_format=ImportBatch.Format.GEOJSON,
        import_mode=ImportBatch.Mode.MERGE,
        original_filename="hydrants.geojson",
        upload_sha256="a" * 64,
        staging_key="hydrants-test",
        actor=admin,
    )
    ImportBatch.objects.create(
        domain=ImportBatch.Domain.PERSONNEL,
        department=department,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.UPSERT,
        original_filename="personnel.csv",
        upload_sha256="b" * 64,
        staging_key="personnel-test",
        actor=admin,
    )
    hydrant_import = client.get(
        reverse("ingestion-imports", args=(department.id,)), {"domain": "hydrants"}
    )
    assert hydrant_import.status_code == 200
    assert hydrant_import.context["target_domain"] == ImportBatch.Domain.HYDRANTS
    assert list(hydrant_import.context["batches"].values_list("domain", flat=True)) == ["hydrants"]
    assert list(hydrant_import.context["form"].fields["domain"].choices) == [
        ("hydrants", "Hydrants")
    ]

    personnel_import = client.get(
        reverse("ingestion-imports", args=(department.id,)), {"domain": "personnel"}
    )
    assert personnel_import.status_code == 200
    assert personnel_import.context["target_domain"] == ImportBatch.Domain.PERSONNEL
    assert list(personnel_import.context["batches"].values_list("domain", flat=True)) == [
        "personnel"
    ]
    assert list(personnel_import.context["form"].fields["domain"].choices) == [
        ("personnel", "Personnel")
    ]
