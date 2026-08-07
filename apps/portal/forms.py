from django import forms


class DepartmentForm(forms.Form):
    name = forms.CharField(max_length=255)
    short_code = forms.CharField(max_length=64)


class StationForm(forms.Form):
    name = forms.CharField(max_length=255)
    short_code = forms.CharField(max_length=64)
    address = forms.CharField(required=False, widget=forms.Textarea)
    active = forms.BooleanField(required=False, initial=True)


class VehicleForm(forms.Form):
    display_name = forms.CharField(max_length=255)
    call_sign = forms.CharField(max_length=128, required=False)
    asset_identifier = forms.CharField(max_length=128, required=False)
    active = forms.BooleanField(required=False, initial=True)


class AdministratorForm(forms.Form):
    email = forms.EmailField(max_length=254)
    display_name = forms.CharField(max_length=255)


class DepartmentStatusForm(forms.Form):
    status = forms.ChoiceField(
        choices=(("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("DEACTIVATED", "Deactivated")),
        required=True,
    )
