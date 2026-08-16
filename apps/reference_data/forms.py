from django import forms


class HydrantImportForm(forms.Form):
    geojson = forms.FileField()


class HydrantForm(forms.Form):
    external_identifier = forms.CharField(max_length=255)
    longitude = forms.FloatField(min_value=-180, max_value=180)
    latitude = forms.FloatField(min_value=-90, max_value=90)
    hydrant_type = forms.CharField(max_length=128, required=False)
    diameter_mm = forms.IntegerField(min_value=1, required=False)
    status = forms.ChoiceField(
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive"), ("UNKNOWN", "Unknown")],
        required=True,
        initial="ACTIVE",
    )


class FirePlanUploadForm(forms.Form):
    document = forms.FileField()
    external_id = forms.CharField(max_length=255)
    object_name = forms.CharField(max_length=255)
    object_reference = forms.CharField(max_length=255, required=False)
    address = forms.CharField(widget=forms.Textarea)
    postal_code = forms.CharField(max_length=32, required=False)
    city = forms.CharField(max_length=255, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False)

    def clean(self):
        cleaned = super().clean() or {}
        longitude = cleaned.get("longitude")
        latitude = cleaned.get("latitude")
        if (longitude is None) != (latitude is None):
            raise forms.ValidationError("Provide both longitude and latitude, or neither.")
        return cleaned


class KlgvPlanUploadForm(forms.Form):
    document = forms.FileField()
    external_id = forms.CharField(max_length=255)
    title = forms.CharField(max_length=255)
    category = forms.CharField(max_length=128, required=False)


class ActiveForm(forms.Form):
    active = forms.BooleanField(required=False)
