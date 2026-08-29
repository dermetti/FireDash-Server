from typing import TYPE_CHECKING

from django import forms

from apps.organizations.models import Station
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan, PhonebookEntry


def _bootstrap_fields(form: forms.BaseForm) -> None:
    """Apply the project Bootstrap widgets without adding another form library."""
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            css_class = "form-check-input"
        elif isinstance(widget, forms.Select | forms.SelectMultiple):
            css_class = "form-select"
        else:
            css_class = "form-control"
        widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css_class}".strip()


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
    street = forms.CharField(
        max_length=255,
        required=False,
        label="Street",
        help_text="Partial street or house-number match.",
    )
    diameter_mm = forms.IntegerField(min_value=1, required=False, label="Diameter (mm)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class HydrantForm(forms.Form):
    external_identifier = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180)
    latitude = forms.FloatField(min_value=-90, max_value=90)
    street = forms.CharField(max_length=255, required=False)
    house_number = forms.CharField(max_length=32, required=False)
    location = forms.CharField(max_length=255, required=False)
    hydrant_type = forms.CharField(max_length=128, required=False)
    diameter_mm = forms.IntegerField(min_value=1, required=False)
    status = forms.ChoiceField(
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive"), ("UNKNOWN", "Unknown")],
        required=True,
        initial="ACTIVE",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class HydrantEditForm(forms.Form):
    external_identifier = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180)
    latitude = forms.FloatField(min_value=-90, max_value=90)
    street = forms.CharField(max_length=255, required=False)
    house_number = forms.CharField(max_length=32, required=False)
    location = forms.CharField(max_length=255, required=False)
    hydrant_type = forms.CharField(max_length=128, required=False)
    flow_information = forms.CharField(max_length=255, required=False)
    diameter_mm = forms.IntegerField(min_value=1, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


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
        widget=forms.TextInput,
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class KlgvPlanUploadForm(forms.Form):
    document = forms.FileField()
    external_identifier = forms.CharField(max_length=255, required=False)
    object_name = forms.CharField(max_length=255)
    address = forms.CharField(max_length=255)
    postal_code = forms.CharField(max_length=32)
    city = forms.CharField(max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class DocumentFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=255, label="Search")
    active = forms.ChoiceField(
        required=False,
        choices=(("", "All statuses"), ("active", "Active"), ("inactive", "Inactive")),
        initial="active",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class FirePlanFilterForm(DocumentFilterForm):
    location_data = forms.ChoiceField(
        required=False,
        label="Location data",
        choices=(
            ("", "All"),
            ("complete", "Coordinates complete"),
            ("missing", "Missing coordinates"),
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


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
        _bootstrap_fields(self)
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
        fields = ("external_identifier", "object_name", "address", "postal_code", "city")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_fields(self)


class ActiveForm(forms.Form):
    active = forms.BooleanField(required=False)


class PhonebookEntryForm(forms.ModelForm):
    class Meta:
        model = PhonebookEntry
        fields = (
            "station",
            "first_name",
            "last_name",
            "organization_unit",
            "function",
            "phone_number",
        )

    def __init__(self, *args, department, **kwargs):
        # ModelForm calls PhonebookEntry.clean() during is_valid(). Bind the
        # tenant before that validation so a valid selected Station is compared
        # with this department, never with an unsaved None department.
        kwargs.setdefault("instance", PhonebookEntry(department=department))
        super().__init__(*args, **kwargs)
        self.fields["station"].queryset = department.stations.order_by("short_code", "name")
        self.fields["station"].required = False
        _bootstrap_fields(self)


class PhonebookFilterForm(forms.Form):
    q = forms.CharField(max_length=255, required=False, label="Search")
    scope = forms.ChoiceField(
        required=False,
        choices=(
            ("", "All scopes"),
            ("department", "Department-wide"),
            ("station", "Station-specific"),
        ),
    )
    station = forms.ModelChoiceField(
        queryset=Station.objects.none(), required=False, label="Station"
    )

    def __init__(self, *args, department, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["station"].queryset = department.stations.order_by("short_code", "name")
        _bootstrap_fields(self)
