from django import forms

from apps.tablets.versions import AppVersionError, parse_app_version


class DepartmentForm(forms.Form):
    name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "form-control"}))
    short_code = forms.CharField(
        max_length=64, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class StationForm(forms.Form):
    name = forms.CharField(max_length=255)
    short_code = forms.CharField(max_length=64)
    street = forms.CharField(max_length=255, required=False)
    house_number = forms.CharField(max_length=32, required=False)
    postal_code = forms.CharField(max_length=32, required=False)
    city = forms.CharField(max_length=255, required=False)
    active = forms.BooleanField(required=False, initial=True)


class VehicleForm(forms.Form):
    display_name = forms.CharField(max_length=255)
    call_sign = forms.CharField(max_length=128, required=False)
    asset_identifier = forms.CharField(max_length=128, required=False)
    active = forms.BooleanField(required=False, initial=True)


class AdministratorForm(forms.Form):
    email = forms.EmailField(
        max_length=254, widget=forms.EmailInput(attrs={"class": "form-control"})
    )
    display_name = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class StationScopeForm(forms.Form):
    user_id = forms.UUIDField()
    station_id = forms.UUIDField()


class RevokeStationScopeForm(forms.Form):
    assignment_id = forms.UUIDField()


class DepartmentStatusForm(forms.Form):
    status = forms.ChoiceField(
        choices=(("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("DEACTIVATED", "Deactivated")),
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class DepartmentTabletLeaseForm(forms.Form):
    tablet_lease_days = forms.IntegerField(
        min_value=3,
        max_value=365,
        label="Maximum offline authorization lease (days)",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )


class DepartmentSystemSettingsForm(DepartmentTabletLeaseForm):
    retention_days = forms.IntegerField(
        min_value=1,
        max_value=36500,
        label="Personnel retention period after offboarding (days)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ApiVersionCompatibilityPolicyForm(forms.Form):
    minimum_app_version = forms.CharField(
        max_length=64,
        required=False,
        label="Minimum supported FireDash app version",
        help_text="Leave blank to allow all app versions using this API generation.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean_minimum_app_version(self):
        value = self.cleaned_data["minimum_app_version"].strip()
        if not value:
            return None
        try:
            return str(parse_app_version(value))
        except AppVersionError as error:
            raise forms.ValidationError(str(error)) from error
