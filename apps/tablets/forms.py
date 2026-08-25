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
        self.auto_asset_number_generation_enabled = bool(
            department and department.tablet_asset_number_auto_enabled
        )
        if self.auto_asset_number_generation_enabled:
            from apps.tablets.services import tablet_asset_number_preview

            preview = tablet_asset_number_preview(department=department)
            self.asset_number_preview = preview
            self.fields["asset_number"].required = False
            self.fields["asset_number"].widget.attrs["readonly"] = "readonly"
            self.fields["asset_number"].widget.attrs["aria-readonly"] = "true"
            self.fields["asset_number"].initial = preview
            # A browser can still submit a different readonly value. Replace it
            # before binding so errors never echo an untrusted manual override.
            if self.is_bound:
                data = self.data.copy()
                data["asset_number"] = preview
                self.data = data

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
        if self.auto_asset_number_generation_enabled:
            # The service will allocate the actual value transactionally; this
            # displayed candidate is only a non-reserved preview.
            return ""
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
