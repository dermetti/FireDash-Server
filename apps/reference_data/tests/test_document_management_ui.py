import hashlib
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications.builders import build_artifact
from apps.publications.feature_services import set_department_feature
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.reference_data.models import FirePlan, KlgvPlan

PDF = b"%PDF-1.4\n1 0 obj\nendobj\n%%EOF\n"
PDF_SHA = hashlib.sha256(PDF).hexdigest()


@pytest.fixture
def document_scope(client, db, tmp_path):
    admin = User.objects.create_user("document-ui@example.test", "Admin", "password")
    department = Department.objects.create(name="Documents", short_code="DOC", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=admin)
    plan = FirePlan.objects.create(
        department=department,
        external_identifier="FP-001",
        object_name="",
        address="Am Stadtrand 56",
        postal_code="22047",
        city="Hamburg",
        location=Point(10.09873774, 53.59229519, srid=4326),
        fsd_location="FSD links vom Eingang",
        bmz_location="Haus 56, erstes Obergeschoss",
        rwa_info="Handtaster EG und oberstes Geschoss",
        document_key="fire-plan.pdf",
        original_filename="plan.pdf",
        file_size=len(PDF),
        page_count=1,
        sha256=PDF_SHA,
        source_pdf_sha256=PDF_SHA,
        uploaded_by=admin,
    )
    klgv = KlgvPlan.objects.create(
        department=department,
        external_identifier="KLGV-001",
        title="KLGV",
        category="Site",
        document_key="klgv.pdf",
        original_filename="klgv.pdf",
        file_size=len(PDF),
        page_count=1,
        source_pdf_sha256=PDF_SHA,
        sanitized_pdf_sha256=PDF_SHA,
        uploaded_by=admin,
    )
    (tmp_path / plan.document_key).write_bytes(PDF)
    (tmp_path / klgv.document_key).write_bytes(PDF)
    client.force_login(admin)
    return admin, department, other, plan, klgv, tmp_path


def _fire_payload(**overrides):
    values = {
        "external_identifier": "FP-001",
        "object_name": "Updated object",
        "address": "Am Stadtrand 56",
        "postal_code": "22047",
        "city": "Hamburg",
        "longitude": "10.123",
        "latitude": "53.456",
        "fsd_location": "FSD Säule links",
        "bmz_location": "BMZ, 1. Obergeschoss",
        "rwa_info": "RWA Handtaster",
    }
    values.update(overrides)
    return values


@pytest.mark.django_db(transaction=True)
def test_fire_plan_ui_edit_lifecycle_delete_and_publication(client, document_scope):
    _, department, _, plan, _, accepted_root = document_scope
    with override_settings(REFERENCE_DATA_ACCEPTED_ROOT=accepted_root):
        FirePlan.objects.bulk_create(
            [
                FirePlan(
                    department=department,
                    external_identifier=f"FP-{index + 1000}",
                    document_key=f"bulk-{index}.pdf",
                    original_filename="bulk.pdf",
                    file_size=1,
                    page_count=1,
                    sha256="a" * 64,
                    uploaded_by=plan.uploaded_by,
                )
                for index in range(100)
            ]
        )
        FirePlan.objects.create(
            department=department,
            external_identifier="INACTIVE",
            document_key="inactive.pdf",
            original_filename="inactive.pdf",
            file_size=1,
            page_count=1,
            sha256="a" * 64,
            active=False,
            uploaded_by=plan.uploaded_by,
        )
        listing = client.get(reverse("reference-data-fire-plans", args=(department.id,)))
        assert listing.status_code == 200
        assert len(listing.context["fire_plans"]) == 100
        assert listing.context["total_count"] == 101
        assert b"table-responsive" in listing.content and b"Page 1 of 2" in listing.content
        first_visible = listing.context["fire_plans"][0]
        assert (
            reverse("reference-data-fire-plan-detail", args=(first_visible.id,)).encode()
            in listing.content
        )
        assert b"INACTIVE" not in listing.content
        inactive = client.get(
            reverse("reference-data-fire-plans", args=(department.id,)), {"active": "inactive"}
        )
        assert [row.external_identifier for row in inactive.context["fire_plans"]] == ["INACTIVE"]

        detail = client.get(reverse("reference-data-fire-plan-detail", args=(plan.id,)))
        assert all(
            value.encode() in detail.content
            for value in (plan.fsd_location, plan.bmz_location, plan.rwa_info)
        )
        assert all(
            label in detail.content.decode()
            for label in ("Edit Data", "Delete Data", "Mark inactive")
        )
        detail_body = detail.content.decode()
        assert "fire-plan-action-modal-container" in detail_body
        assert 'data-bs-toggle="modal"' not in detail_body
        assert "htmx:afterSwap" in detail_body
        edit_url = reverse("reference-data-fire-plan-edit", args=(plan.id,))
        edit_get = client.get(edit_url, HTTP_HX_REQUEST="true")
        assert edit_get.status_code == 200
        assert b'<div class="modal fade"' in edit_get.content
        assert b'<div class="modal-dialog modal-lg"' in edit_get.content
        assert b'<div class="modal-content"' in edit_get.content
        assert b'hx-target="#fire-plan-action-modal-container"' in edit_get.content
        assert b'type="submit"' in edit_get.content
        invalid = client.post(
            edit_url, _fire_payload(longitude="999", fsd_location="Entered value")
        )
        assert invalid.status_code == 200 and b"Entered value" in invalid.content
        plan.refresh_from_db()
        assert plan.fsd_location == "FSD links vom Eingang"
        assert client.post(edit_url, _fire_payload()).status_code == 302
        plan.refresh_from_db()
        assert (plan.fsd_location, plan.bmz_location, plan.rwa_info) == (
            "FSD Säule links",
            "BMZ, 1. Obergeschoss",
            "RWA Handtaster",
        )
        assert AuditEvent.objects.filter(
            action="reference_data.fire_plan_updated", target_uuid=plan.id
        ).exists()
        assert PublicationJob.objects.filter(
            department=department, dataset_type_code="department_fire_plans"
        ).exists()
        htmx_success = client.post(edit_url, _fire_payload(), HTTP_HX_REQUEST="true")
        assert htmx_success.status_code == 204
        assert htmx_success["HX-Redirect"] == reverse(
            "reference-data-fire-plan-detail", args=(plan.id,)
        )
        assert (
            client.get(reverse("reference-data-fire-plan-detail", args=(uuid.uuid4(),))).status_code
            == 404
        )

        assert (
            client.post(
                reverse("reference-data-fire-plan-lifecycle", args=(plan.id,)), {"active": "false"}
            ).status_code
            == 302
        )
        plan.refresh_from_db()
        assert plan.active is False and FirePlan.objects.filter(pk=plan.id).exists()
        FirePlan.objects.filter(department=department).exclude(pk=plan.id).update(active=False)
        artifact = build_artifact(
            definition=get_dataset_definition("department_fire_plans"),
            department=department,
            station=None,
            source_revision=1,
        )
        assert str(plan.id).encode() not in artifact
        assert (
            client.post(
                reverse("reference-data-fire-plan-lifecycle", args=(plan.id,)), {"active": "true"}
            ).status_code
            == 302
        )

        delete_url = reverse("reference-data-fire-plan-delete", args=(plan.id,))
        assert client.get(delete_url, HTTP_HX_REQUEST="true").status_code == 200
        assert (accepted_root / plan.document_key).exists()
        assert client.post(delete_url).status_code == 302
        assert not FirePlan.objects.filter(pk=plan.id).exists()
        assert not (accepted_root / plan.document_key).exists()
        assert AuditEvent.objects.filter(
            action="reference_data.fire_plan_deleted", target_uuid=plan.id
        ).exists()
        assert DatasetScopeState.objects.filter(
            department=department, dataset_type_code="department_fire_plans"
        ).exists()
        assert (
            client.get(reverse("reference-data-fire-plan-detail", args=(plan.id,))).status_code
            == 404
        )


@pytest.mark.django_db(transaction=True)
def test_klgv_ui_edit_lifecycle_delete_and_scope(client, document_scope):
    actor, department, _, _, plan, accepted_root = document_scope
    set_department_feature(
        actor=actor, department=department, feature_code="klgv_plans", enabled=True
    )
    with override_settings(REFERENCE_DATA_ACCEPTED_ROOT=accepted_root):
        KlgvPlan.objects.bulk_create(
            [
                KlgvPlan(
                    department=department,
                    external_identifier=f"K-{index + 1000}",
                    title=f"KLGV {index:03}",
                    document_key=f"bulk-k-{index}.pdf",
                    original_filename="bulk.pdf",
                    file_size=1,
                    page_count=1,
                    source_pdf_sha256="a" * 64,
                    sanitized_pdf_sha256="a" * 64,
                    uploaded_by=plan.uploaded_by,
                )
                for index in range(100)
            ]
        )
        listing = client.get(reverse("reference-data-klgv-plans", args=(department.id,)))
        assert listing.status_code == 200 and len(listing.context["plans"]) == 100
        assert listing.context["total_count"] == 101
        assert (
            reverse("reference-data-klgv-plan-detail", args=(plan.id,)).encode() in listing.content
        )
        detail = client.get(reverse("reference-data-klgv-plan-detail", args=(plan.id,)))
        assert (
            detail.status_code == 200
            and b"Edit Data" in detail.content
            and b"Delete Data" in detail.content
        )
        detail_body = detail.content.decode()
        assert "klgv-action-modal-container" in detail_body
        assert 'data-bs-toggle="modal"' not in detail_body
        assert "htmx:afterSwap" in detail_body
        edit_url = reverse("reference-data-klgv-plan-edit", args=(plan.id,))
        edit_get = client.get(edit_url, HTTP_HX_REQUEST="true")
        assert edit_get.status_code == 200
        assert b'<div class="modal fade"' in edit_get.content
        assert b'<div class="modal-dialog"' in edit_get.content
        assert b'<div class="modal-content"' in edit_get.content
        assert b'hx-target="#klgv-action-modal-container"' in edit_get.content
        assert b'type="submit"' in edit_get.content
        invalid = client.post(
            edit_url, {"external_identifier": "", "title": "Entered", "category": ""}
        )
        assert invalid.status_code == 200 and b"Entered" in invalid.content
        assert (
            client.post(
                edit_url,
                {"external_identifier": "KLGV-001", "title": "Updated", "category": "Operational"},
            ).status_code
            == 302
        )
        plan.refresh_from_db()
        assert (plan.title, plan.category) == ("Updated", "Operational")
        assert AuditEvent.objects.filter(
            action="reference_data.klgv_plan_updated", target_uuid=plan.id
        ).exists()
        assert PublicationJob.objects.filter(
            department=department, dataset_type_code="department_klgv_plans"
        ).exists()
        htmx_success = client.post(
            edit_url,
            {"external_identifier": "KLGV-001", "title": "Updated", "category": "Operational"},
            HTTP_HX_REQUEST="true",
        )
        assert htmx_success.status_code == 204
        assert htmx_success["HX-Redirect"] == reverse(
            "reference-data-klgv-plan-detail", args=(plan.id,)
        )
        assert (
            client.post(
                reverse("reference-data-klgv-plan-lifecycle", args=(plan.id,)), {"active": "false"}
            ).status_code
            == 302
        )
        plan.refresh_from_db()
        assert plan.active is False and KlgvPlan.objects.filter(pk=plan.id).exists()
        delete_url = reverse("reference-data-klgv-plan-delete", args=(plan.id,))
        assert client.get(delete_url, HTTP_HX_REQUEST="true").status_code == 200
        assert client.post(delete_url).status_code == 302
        assert not KlgvPlan.objects.filter(pk=plan.id).exists()
        assert not (accepted_root / plan.document_key).exists()
        assert AuditEvent.objects.filter(
            action="reference_data.klgv_plan_deleted", target_uuid=plan.id
        ).exists()


@pytest.mark.django_db
def test_document_import_pages_are_domain_scoped(client, document_scope):
    _, department, _, _, _, _ = document_scope
    fire = client.get(reverse("ingestion-imports", args=(department.id,)), {"domain": "fire_plans"})
    klgv = client.get(reverse("ingestion-imports", args=(department.id,)), {"domain": "klgv_plans"})
    assert fire.status_code == klgv.status_code == 200
    assert list(fire.context["form"].fields["domain"].choices) == [("fire_plans", "Fire plans")]
    assert b"Fire Plan manifest CSV" in fire.content and b"KLGV manifest CSV" not in fire.content
    assert list(klgv.context["form"].fields["domain"].choices) == [("klgv_plans", "KLGV plans")]
    assert b"KLGV manifest CSV" in klgv.content and b"Fire Plan manifest CSV" not in klgv.content
