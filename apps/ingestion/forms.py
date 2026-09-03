# ruff: noqa: E501
from django import forms

from apps.ingestion.models import ImportBatch
from apps.organizations.models import Station


class ImportUploadForm(forms.Form):
    domain = forms.ChoiceField(
        choices=(
            (ImportBatch.Domain.HYDRANTS, "Hydrants"),
            (ImportBatch.Domain.PERSONNEL, "Personnel"),
            (ImportBatch.Domain.FIRE_PLANS, "Fire-plan ZIP package"),
            (ImportBatch.Domain.KLGV_PLANS, "KLGV ZIP package"),
            (ImportBatch.Domain.STATION_VEHICLES, "Stations and vehicles"),
            (ImportBatch.Domain.PHONEBOOK, "Phonebook"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    import_format = forms.ChoiceField(
        choices=(
            (ImportBatch.Format.CSV, "CSV"),
            (ImportBatch.Format.JSON, "JSON"),
            (ImportBatch.Format.GEOJSON, "GeoJSON"),
            (ImportBatch.Format.ZIP, "PDF ZIP package"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    import_mode = forms.ChoiceField(
        choices=(
            (ImportBatch.Mode.MERGE, "Merge"),
            (ImportBatch.Mode.UPSERT, "Upsert"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    source = forms.FileField(widget=forms.ClearableFileInput(attrs={"class": "form-control"}))
    station = forms.ModelChoiceField(
        queryset=Station.objects.none(),
        required=False,
        help_text="Required for new personnel.",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean(self):
        cleaned = super().clean() or {}
        domain = cleaned.get("domain")
        import_format = cleaned.get("import_format")
        mode = cleaned.get("import_mode")
        allowed: dict[str, dict[str, set[str]]] = {
            ImportBatch.Domain.HYDRANTS: {
                "formats": {
                    ImportBatch.Format.CSV,
                    ImportBatch.Format.GEOJSON,
                },
                "modes": {ImportBatch.Mode.MERGE},
            },
            ImportBatch.Domain.PERSONNEL: {
                "formats": {ImportBatch.Format.CSV},
                "modes": {ImportBatch.Mode.UPSERT},
            },
            ImportBatch.Domain.FIRE_PLANS: {
                "formats": {ImportBatch.Format.ZIP},
                "modes": {ImportBatch.Mode.UPSERT},
            },
            ImportBatch.Domain.KLGV_PLANS: {
                "formats": {ImportBatch.Format.ZIP},
                "modes": {ImportBatch.Mode.UPSERT},
            },
            ImportBatch.Domain.STATION_VEHICLES: {
                "formats": {ImportBatch.Format.CSV},
                "modes": {ImportBatch.Mode.UPSERT},
            },
            ImportBatch.Domain.PHONEBOOK: {
                "formats": {ImportBatch.Format.CSV},
                "modes": {ImportBatch.Mode.UPSERT},
            },
        }
        rule = allowed.get(domain) if isinstance(domain, str) else None
        if rule is None or import_format not in rule["formats"] or mode not in rule["modes"]:
            raise forms.ValidationError("Selected domain, format, and mode are not compatible.")
        return cleaned


class DangerousGoodsUploadForm(forms.Form):
    """One curated JSON source; its contents are validated by the import service."""

    source = forms.FileField(
        label="Curated dangerous-goods JSON",
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".json,application/json"}
        ),
    )

    def clean_source(self):
        source = self.cleaned_data["source"]
        if not source.name.lower().endswith(".json"):
            raise forms.ValidationError("Upload the curated dangerous_goods_v1.json file.")
        return source


class FirePlanCoordinateReviewForm(forms.Form):
    """Validate optional Fire Plan coordinate completion in staged review data."""

    longitude = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "step": "any", "inputmode": "decimal"}
        ),
    )
    latitude = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "step": "any", "inputmode": "decimal"}
        ),
    )

    def __init__(self, *args, longitude: object, latitude: object, **kwargs) -> None:
        super().__init__(
            *args,
            initial={"longitude": longitude, "latitude": latitude},
            **kwargs,
        )
        self._existing_coordinates = {"longitude": longitude, "latitude": latitude}

    def clean(self):
        cleaned = super().clean()
        for field in ("longitude", "latitude"):
            if field in self.errors:
                continue
            if cleaned.get(field) is None:
                existing = self._existing_coordinates[field]
                if existing is None:
                    self.add_error(
                        field, "This coordinate is required to complete the staged record."
                    )
                else:
                    cleaned[field] = existing
        return cleaned


class StationVehicleResolutionForm(forms.Form):
    """Stage an import-only Station resolution without creating canonical data."""

    station_id = forms.ModelChoiceField(
        queryset=Station.objects.none(),
        required=False,
        label="Resolve to existing Station",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    short_code = forms.CharField(
        max_length=64,
        required=False,
        label="Short Code",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    name = forms.CharField(
        max_length=255,
        required=False,
        label="Station name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    street = forms.CharField(
        max_length=255,
        required=False,
        label="Street",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    house_number = forms.CharField(
        max_length=32,
        required=False,
        label="House number",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    postal_code = forms.CharField(
        max_length=32,
        required=False,
        label="Postal code",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    city = forms.CharField(
        max_length=255,
        required=False,
        label="City",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, department, resolution_kind: str, initial=None, **kwargs):
        super().__init__(*args, initial=initial, **kwargs)
        self.resolution_kind = resolution_kind
        self.fields["station_id"].queryset = Station.objects.filter(
            department=department, active=True
        ).order_by("name", "short_code", "id")
        if resolution_kind == "ambiguous":
            self.fields["station_id"].required = True

    def clean(self):
        cleaned = super().clean()
        if self.resolution_kind == "missing":
            for field in ("short_code", "name"):
                if not cleaned.get(field, "").strip():
                    self.add_error(field, "This field is required to stage the missing Station.")
        return cleaned
