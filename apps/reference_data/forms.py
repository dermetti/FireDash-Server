from typing import TYPE_CHECKING

from django import forms

from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan

if TYPE_CHECKING:
    _FirePlanModelForm = forms.ModelForm[FirePlan]
    _KlgvPlanModelForm = forms.ModelForm[KlgvPlan]
else:
    _FirePlanModelForm = forms.ModelForm
    _KlgvPlanModelForm = forms.ModelForm


class HydrantImportForm(forms.Form):
    geojson = forms.FileField()


class HydrantFilterForm(forms.Form):
    """Server-side filters for the bounded Hydrant list."""

    q = forms.CharField(
        max_length=255,
        required=False,
        label="Identifier",
        help_text="Partial identifier match.",
    )
    status = forms.ChoiceField(
        choices=[("", "All statuses"), *Hydrant.Status.choices],
        required=False,
        initial=Hydrant.Status.ACTIVE,
    )
    hydrant_type = forms.CharField(
        max_length=128,
        required=False,
        label="Hydrant type",
        help_text="Partial type match.",
    )
    diameter_mm = forms.IntegerField(min_value=1, required=False, label="Diameter (mm)")


class HydrantForm(forms.Form):
    external_identifier = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180)
    latitude = forms.FloatField(min_value=-90, max_value=90)
    hydrant_type = forms.CharField(max_length=128, required=False)
    diameter_mm = forms.IntegerField(min_value=1, required=False)
    status = forms.ChoiceField(
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive"), ("UNKNOWN", "Unknown")],
        required=True,
        initial="ACTIVE",
    )


class HydrantEditForm(forms.Form):
    external_identifier = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180)
    latitude = forms.FloatField(min_value=-90, max_value=90)
    hydrant_type = forms.CharField(max_length=128, required=False)
    flow_information = forms.CharField(max_length=255, required=False)
    diameter_mm = forms.IntegerField(min_value=1, required=False)


class FirePlanUploadForm(forms.Form):
    document = forms.FileField()
    external_identifier = forms.CharField(
        max_length=255,
        required=False,
        help_text="Optional stable identifier supplied by the department.",
    )
    object_name = forms.CharField(max_length=255, required=False)
    address = forms.CharField(
        required=False,
        widget=forms.Textarea,
        help_text=(
            "Required when no External ID is available. When used without an External ID, "
            "the address identifies the Fire Plan."
        ),
    )
    postal_code = forms.CharField(max_length=32, required=False)
    city = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False)
    fsd_location = forms.CharField(required=False, widget=forms.Textarea)
    bmz_location = forms.CharField(required=False, widget=forms.Textarea)
    rwa_info = forms.CharField(required=False, widget=forms.Textarea)

    def clean(self):
        cleaned = super().clean() or {}
        external_identifier = (cleaned.get("external_identifier") or "").strip()
        address = (cleaned.get("address") or "").strip()
        cleaned["external_identifier"] = external_identifier
        cleaned["address"] = address
        if not external_identifier and not address:
            self.add_error("address", "Address is required when no External ID is available.")
        longitude = cleaned.get("longitude")
        latitude = cleaned.get("latitude")
        if (longitude is None) != (latitude is None):
            raise forms.ValidationError("Provide both longitude and latitude, or neither.")
        return cleaned


class KlgvPlanUploadForm(forms.Form):
    document = forms.FileField()
    external_id = forms.CharField(max_length=255)
    title = forms.CharField(max_length=255)
    category = forms.CharField(max_length=128, required=False)


class DocumentFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=255, label="Search")
    active = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), ("active", "Active"), ("inactive", "Inactive")),
        initial="active",
    )


class FirePlanEditForm(_FirePlanModelForm):
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False)

    class Meta:
        model = FirePlan
        fields = (
            "external_identifier",
            "object_name",
            "address",
            "postal_code",
            "city",
            "fsd_location",
            "bmz_location",
            "rwa_info",
        )
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "fsd_location": forms.Textarea(attrs={"rows": 2}),
            "bmz_location": forms.Textarea(attrs={"rows": 2}),
            "rwa_info": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.location:
            self.initial.setdefault("longitude", self.instance.location.x)
            self.initial.setdefault("latitude", self.instance.location.y)

    def clean(self):
        cleaned = super().clean() or {}
        if (
            not (cleaned.get("external_identifier") or "").strip()
            and not (cleaned.get("address") or "").strip()
        ):
            self.add_error("address", "Address is required when no External ID is available.")
        if (cleaned.get("longitude") is None) != (cleaned.get("latitude") is None):
            raise forms.ValidationError("Provide both longitude and latitude, or neither.")
        return cleaned


class KlgvPlanEditForm(_KlgvPlanModelForm):
    class Meta:
        model = KlgvPlan
        fields = ("external_identifier", "title", "category")


class ActiveForm(forms.Form):
    active = forms.BooleanField(required=False)
