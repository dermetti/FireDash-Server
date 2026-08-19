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
        )
    )
    import_format = forms.ChoiceField(
        choices=(
            (ImportBatch.Format.CSV, "CSV"),
            (ImportBatch.Format.JSON, "JSON"),
            (ImportBatch.Format.GEOJSON, "GeoJSON"),
            (ImportBatch.Format.ZIP, "PDF ZIP package"),
        )
    )
    import_mode = forms.ChoiceField(
        choices=(
            (ImportBatch.Mode.MERGE, "Merge"),
            (ImportBatch.Mode.UPSERT, "Upsert"),
        )
    )
    source = forms.FileField()
    station = forms.ModelChoiceField(
        queryset=Station.objects.none(), required=False, help_text="Required for new personnel."
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
                    ImportBatch.Format.JSON,
                    ImportBatch.Format.GEOJSON,
                },
                "modes": {ImportBatch.Mode.MERGE},
            },
            ImportBatch.Domain.PERSONNEL: {
                "formats": {ImportBatch.Format.CSV, ImportBatch.Format.JSON},
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
        }
        rule = allowed.get(domain) if isinstance(domain, str) else None
        if rule is None or import_format not in rule["formats"] or mode not in rule["modes"]:
            raise forms.ValidationError("Selected domain, format, and mode are not compatible.")
        return cleaned
