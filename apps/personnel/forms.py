from django import forms

from apps.personnel.models import Person


class PersonForm(forms.Form):
    personnel_number = forms.CharField(max_length=128)
    first_name = forms.CharField(max_length=128)
    last_name = forms.CharField(max_length=128)


class PersonnelFilterForm(forms.Form):
    q = forms.CharField(max_length=255, required=False, label="Search")
    status = forms.ChoiceField(
        choices=[("", "All statuses"), *Person.LifecycleStatus.choices],
        required=False,
        initial=Person.LifecycleStatus.ACTIVE,
    )
    home_station = forms.UUIDField(required=False)


class CommanderEligibilityForm(forms.Form):
    eligible = forms.BooleanField(required=False)


class CommanderEmailForm(forms.Form):
    email = forms.EmailField(max_length=254)


class RetentionPolicyForm(forms.Form):
    retention_days = forms.IntegerField(min_value=1, max_value=36500)
