from django import forms


class PersonForm(forms.Form):
    personnel_number = forms.CharField(max_length=128, required=False)
    first_name = forms.CharField(max_length=128)
    last_name = forms.CharField(max_length=128)


class CommanderEligibilityForm(forms.Form):
    eligible = forms.BooleanField(required=False)


class CommanderEmailForm(forms.Form):
    email = forms.EmailField(max_length=254)


class RetentionPolicyForm(forms.Form):
    retention_days = forms.IntegerField(min_value=1, max_value=36500)
