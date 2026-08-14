from django import forms

from apps.tablets.models import Tablet

_REMOVAL_CHOICES = [
    (Tablet.Status.LOST, Tablet.Status.LOST.label),
    (Tablet.Status.RETIRED, Tablet.Status.RETIRED.label),
    (Tablet.Status.REMOVED, Tablet.Status.REMOVED.label),
]


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


class TabletVehicleAssignmentForm(forms.Form):
    vehicle_id = forms.UUIDField()


class TabletRemovalForm(forms.Form):
    status = forms.ChoiceField(
        choices=_REMOVAL_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        max_length=512,
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
