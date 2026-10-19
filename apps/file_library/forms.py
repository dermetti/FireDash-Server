from typing import cast

from django import forms

from apps.organizations.models import Station


class MultiplePDFInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiplePDFField(forms.FileField):
    def clean(self, data, initial=None):
        uploads = data if isinstance(data, list | tuple) else [data]
        if not uploads:
            return super().clean(None, initial)
        return [super(MultiplePDFField, self).clean(upload, initial) for upload in uploads]


class ExplorerActionForm(forms.Form):
    """Focused transport validation; library services enforce all domain rules."""

    def __init__(self, *args, action, **kwargs):
        super().__init__(*args, **kwargs)
        if action in {"new-folder", "rename"}:
            self.fields["name"] = forms.CharField(
                max_length=255, label="Folder name" if action == "new-folder" else "New name"
            )
        elif action == "upload":
            self.fields["pdfs"] = MultiplePDFField(
                label="Choose PDF files",
                widget=MultiplePDFInput(attrs={"accept": ".pdf,application/pdf"}),
            )
        elif action == "replace":
            self.fields["pdf"] = forms.FileField(
                label="Replacement PDF",
                widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}),
            )
        elif action == "delete":
            self.fields["confirm"] = forms.BooleanField(
                label="I understand this deletion cannot be undone from the file explorer.",
                widget=forms.CheckboxInput(
                    attrs={"class": "form-check-input"}, check_test=lambda v: v == "yes"
                ),
            )
            self.fields["confirm"].widget.attrs["value"] = "yes"
        for name, field in self.fields.items():
            if name != "confirm":
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean() or {}
        if "confirm" in self.fields and self.data.get("confirm") != "yes":
            self.add_error("confirm", "Confirm deletion explicitly.")
        return cleaned


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
