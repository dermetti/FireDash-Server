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

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.department = department

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


class TabletVehicleAssignmentForm(forms.Form):
    vehicle_id = forms.UUIDField()


class TabletReasonForm(forms.Form):
    reason = forms.CharField(
        max_length=512,
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
