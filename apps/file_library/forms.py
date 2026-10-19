from typing import cast

from django import forms

from apps.organizations.models import Station


class TemporaryStationAccessForm(forms.Form):
    station_id = forms.ModelChoiceField(queryset=Station.objects.none(), label="Station")
    confirm = forms.BooleanField(required=True)

    def __init__(self, *args, department_id, query="", **kwargs):
        super().__init__(*args, **kwargs)
        stations = Station.objects.filter(department_id=department_id, active=True)
        cast("forms.ModelChoiceField[Station]", self.fields["station_id"]).queryset = stations
        results = stations
        if query:
            from django.db.models import Q

            results = results.filter(Q(name__icontains=query) | Q(short_code__icontains=query))
        options = list(results.order_by("name", "id")[:25])
        selected = self.data.get("station_id") if self.is_bound else self.initial.get("station_id")
        try:
            choice = stations.filter(pk=selected).first() if selected else None
        except (ValueError, forms.ValidationError):
            choice = None
        if choice and choice not in options:
            options.append(choice)
        self.options = [
            (str(station.id), f"{station.name} ({station.short_code})") for station in options
        ]

    def clean_confirm(self):
        if self.data.get("confirm") != "yes":
            raise forms.ValidationError("Confirm temporary station access explicitly.")
        return True
