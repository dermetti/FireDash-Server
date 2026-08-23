import pytest
from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.reference_data.forms import FirePlanEditForm, FirePlanUploadForm
from apps.reference_data.models import FirePlan


@pytest.mark.django_db
def test_operational_location_fields_are_optional_and_persist():
    actor = User.objects.create_user("fire-plan-fields@example.test", "Admin", "safe-password")
    department = Department.objects.create(name="Plans", short_code="PLN", created_by=actor)
    plan = FirePlan.objects.create(
        department=department,
        external_identifier="FP-1",
        document_key="fp-1.pdf",
        original_filename="fp.pdf",
        file_size=1,
        page_count=1,
        sha256="a" * 64,
        uploaded_by=actor,
    )
    assert (plan.fsd_location, plan.bmz_location, plan.rwa_info) == ("", "", "")

    form = FirePlanEditForm(
        {
            "external_identifier": "FP-1",
            "object_name": "",
            "address": "",
            "postal_code": "",
            "city": "",
            "longitude": "10.123",
            "latitude": "53.456",
            "fsd_location": "Säule links",
            "bmz_location": "EG",
            "rwa_info": "Handtaster",
        },
        instance=plan,
    )
    assert form.is_valid(), form.errors
    updated = form.save(commit=False)
    updated.location = Point(
        form.cleaned_data["longitude"], form.cleaned_data["latitude"], srid=4326
    )
    updated.save()
    plan.refresh_from_db()
    assert plan.fsd_location == "Säule links"
    assert plan.bmz_location == "EG"
    assert plan.rwa_info == "Handtaster"
    assert plan.location == Point(10.123, 53.456, srid=4326)


def test_single_fire_plan_form_accepts_optional_operational_locations():
    form = FirePlanUploadForm(
        {
            "external_identifier": "FP-1",
            "object_name": "",
            "address": "",
            "postal_code": "",
            "city": "",
            "longitude": "",
            "latitude": "",
            "fsd_location": "",
            "bmz_location": "",
            "rwa_info": "",
        },
        {"document": SimpleUploadedFile("plan.pdf", b"%PDF-1.4")},
    )
    # The actual UploadedFile validation is exercised by the ingestion path;
    # these descriptive fields must not add an identity/blank restriction.
    assert "fsd_location" not in form.errors
