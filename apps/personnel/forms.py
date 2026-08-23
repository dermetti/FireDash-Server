from django import forms

from apps.personnel.models import Person


class PersonForm(forms.Form):
    personnel_number = forms.CharField(
        max_length=128, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    first_name = forms.CharField(
        max_length=128, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    last_name = forms.CharField(
        max_length=128, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class PersonnelFilterForm(forms.Form):
    q = forms.CharField(
        max_length=255,
        required=False,
        label="Search",
        widget=forms.SearchInput(
            attrs={"class": "form-control", "placeholder": "Search name or personnel number"}
        ),
    )
    status = forms.ChoiceField(
        choices=[("", "All statuses"), *Person.LifecycleStatus.choices],
        required=False,
        initial=Person.LifecycleStatus.ACTIVE,
    )
    home_station = forms.UUIDField(
        required=False, widget=forms.Select(attrs={"class": "form-select"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].widget.attrs["class"] = "form-select"


class CommanderEligibilityForm(forms.Form):
    eligible = forms.BooleanField(required=False)


class CommanderEmailForm(forms.Form):
    email = forms.EmailField(max_length=254)


class RetentionPolicyForm(forms.Form):
    retention_days = forms.IntegerField(min_value=1, max_value=36500)
