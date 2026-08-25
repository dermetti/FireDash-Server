from django import forms

from apps.tablets.models import Tablet


class TabletForm(forms.Form):
    display_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "aria-describedby": "tablet-display-name-help"}
        ),
    )
    asset_number = forms.CharField(
        max_length=128,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "aria-describedby": "tablet-asset-number-help"}
        ),
    )
    generate_asset_number = forms.BooleanField(
        required=False,
        label="Generate automatically",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.department = department
        self.auto_asset_number_generation_enabled = bool(
            department and department.tablet_asset_number_auto_enabled
        )

    def clean_display_name(self):
        name = self.cleaned_data["display_name"].strip()
        if not name:
            raise forms.ValidationError("Display name is required.")
        if (
            self.department is not None
            and Tablet.objects.filter(department=self.department, display_name=name).exists()
        ):
            raise forms.ValidationError(
                "A tablet with this display name already exists in the department."
            )
        return name

    def clean_asset_number(self):
        asset = self.cleaned_data.get("asset_number", "").strip()
        if (
            self.department is not None
            and asset
            and Tablet.objects.filter(department=self.department, asset_number=asset).exists()
        ):
            raise forms.ValidationError(
                "A tablet with this asset number already exists in the department."
            )
        return asset

    def clean(self):
        cleaned_data = super().clean()
        generate_asset_number = cleaned_data.get("generate_asset_number", False)
        asset_number = cleaned_data.get("asset_number", "")
        if generate_asset_number and not self.auto_asset_number_generation_enabled:
            self.add_error(
                "generate_asset_number",
                "Automatic asset-number generation is not enabled for this Department.",
            )
        if generate_asset_number and asset_number:
            self.add_error(
                "asset_number",
                "Choose automatic generation or enter a manual asset number.",
            )
        return cleaned_data


class TabletVehicleAssignmentForm(forms.Form):
    vehicle_id = forms.UUIDField()


class TabletReasonForm(forms.Form):
    reason = forms.CharField(
        max_length=512,
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
