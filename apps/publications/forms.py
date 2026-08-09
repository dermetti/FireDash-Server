from typing import cast

from django import forms
from django.forms import ChoiceField, ModelChoiceField

from apps.organizations.models import Station
from apps.publications.registry import DATASET_REGISTRY, production_dataset_definitions


class RebuildRequestForm(forms.Form):
    dataset_type_code = forms.ChoiceField(label="Dataset")
    station = forms.ModelChoiceField(queryset=Station.objects.none(), required=False)

    def __init__(self, *args, department, **kwargs):
        super().__init__(*args, **kwargs)
        self.department = department
        dataset_type_field = cast(ChoiceField, self.fields["dataset_type_code"])
        station_field = cast("ModelChoiceField[Station]", self.fields["station"])
        dataset_type_field.choices = [
            (definition.code, definition.display_name)
            for definition in production_dataset_definitions()
        ]
        station_field.queryset = Station.objects.filter(department=department, active=True)

    def clean(self):
        cleaned_data = super().clean() or {}
        dataset_type_code = cleaned_data.get("dataset_type_code")
        if not dataset_type_code:
            return cleaned_data
        definition = DATASET_REGISTRY[dataset_type_code]
        station = cleaned_data.get("station")
        if definition.scope == "department" and station is not None:
            self.add_error("station", "Department datasets do not use a station.")
        if definition.scope == "station" and station is None:
            self.add_error("station", "Station datasets require a station.")
        return cleaned_data
