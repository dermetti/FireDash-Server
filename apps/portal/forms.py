from django import forms


class DepartmentForm(forms.Form):
    name = forms.CharField(max_length=255)
    short_code = forms.CharField(max_length=64)


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
    email = forms.EmailField(max_length=254)
    display_name = forms.CharField(max_length=255)


class StationScopeForm(forms.Form):
    user_id = forms.UUIDField()
    station_id = forms.UUIDField()


class RevokeStationScopeForm(forms.Form):
    assignment_id = forms.UUIDField()


class DepartmentStatusForm(forms.Form):
    status = forms.ChoiceField(
        choices=(("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("DEACTIVATED", "Deactivated")),
        required=True,
    )


class DepartmentTabletLeaseForm(forms.Form):
    tablet_lease_days = forms.IntegerField(
        min_value=3, label="Maximum offline authorization lease (days)"
    )
