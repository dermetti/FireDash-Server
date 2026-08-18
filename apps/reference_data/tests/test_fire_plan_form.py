from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.datastructures import MultiValueDict

from apps.reference_data.forms import FirePlanUploadForm


def _form(**values):
    return FirePlanUploadForm(
        data=values,
        files=MultiValueDict({"document": [SimpleUploadedFile("plan.pdf", b"%PDF-1.4\n")]}),
    )


def test_fire_plan_form_accepts_external_identifier_without_address():
    form = _form(external_identifier="PLAN-123", object_name="", address="")
    assert form.is_valid(), form.errors


def test_fire_plan_form_accepts_address_identity_without_external_identifier():
    form = _form(external_identifier="", object_name="", address="Wandsbeker Zollstraße 95")
    assert form.is_valid(), form.errors


def test_fire_plan_form_requires_external_identifier_or_address():
    form = _form(external_identifier="", object_name="", address="")
    assert not form.is_valid()
    assert "address" in form.errors


def test_fire_plan_form_requires_paired_longitude_latitude():
    form = _form(external_identifier="PLAN-123", object_name="", address="", longitude="10.123")
    assert not form.is_valid()
    assert "__all__" in form.errors
