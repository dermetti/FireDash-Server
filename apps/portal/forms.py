from urllib.parse import urlsplit

# ruff: noqa: E501
from django import forms

from apps.authorization.models import validate_vehicle_rescue_guides_web_url
from apps.organizations.presentation import DEPARTMENT_LOCALE_CHOICES, DEPARTMENT_TIMEZONE_CHOICES
from apps.outbound_mail.http_transport import (
    OutboundMailTransportError,
    validate_outbound_mail_https_proxy,
)
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.providers import API_PROVIDER_REGISTRY
from apps.tablets.models import Tablet
from apps.tablets.versions import AppVersionError, parse_app_version


class DepartmentForm(forms.Form):
    name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "form-control"}))
    short_code = forms.CharField(
        max_length=64, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class StationForm(forms.Form):
    name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "form-control"}))
    short_code = forms.CharField(
        max_length=64, label="Short Code", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    street = forms.CharField(
        max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    house_number = forms.CharField(
        max_length=32, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    postal_code = forms.CharField(
        max_length=32, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    city = forms.CharField(
        max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class VehicleForm(forms.Form):
    display_name = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    call_sign = forms.CharField(
        max_length=128, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    asset_identifier = forms.CharField(
        max_length=128, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class StationListFilterForm(forms.Form):
    q = forms.CharField(
        max_length=255,
        required=False,
        label="Search",
        widget=forms.SearchInput(
            attrs={"class": "form-control", "placeholder": "Search name, Short Code, or city"}
        ),
    )
    active = forms.ChoiceField(
        required=False,
        choices=(
            ("", "Current stations"),
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All statuses"),
        ),
        initial="",
        label="Status",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class AdministratorForm(forms.Form):
    email = forms.EmailField(
        max_length=254, widget=forms.EmailInput(attrs={"class": "form-control"})
    )
    display_name = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class AdministratorRemovalForm(forms.Form):
    confirmation = forms.CharField(
        max_length=32, widget=forms.TextInput(attrs={"class": "form-control"})
    )

    def clean_confirmation(self):
        if self.cleaned_data["confirmation"] != "REMOVE":
            raise forms.ValidationError("Type REMOVE to permanently remove this administrator.")
        return "REMOVE"


class StationScopeForm(forms.Form):
    user_id = forms.UUIDField()
    station_id = forms.UUIDField()


class RevokeStationScopeForm(forms.Form):
    assignment_id = forms.UUIDField()


class DepartmentStatusForm(forms.Form):
    status = forms.ChoiceField(
        choices=(("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("DEACTIVATED", "Deactivated")),
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class DepartmentTabletLeaseForm(forms.Form):
    tablet_lease_days = forms.IntegerField(
        min_value=3,
        max_value=365,
        label="Maximum offline authorization lease (days)",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )


class DepartmentSystemSettingsForm(DepartmentTabletLeaseForm):
    retention_days = forms.IntegerField(
        min_value=1,
        max_value=36500,
        label="Personnel retention period after offboarding (days)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class DepartmentPersonnelRetentionForm(forms.Form):
    retention_days = forms.IntegerField(
        min_value=1,
        max_value=36500,
        label="Personnel retention period after offboarding (days)",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )


class DepartmentLocaleTimePolicyForm(forms.Form):
    locale = forms.ChoiceField(
        choices=DEPARTMENT_LOCALE_CHOICES,
        label="Locale",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    timezone = forms.ChoiceField(
        choices=DEPARTMENT_TIMEZONE_CHOICES,
        label="Timezone",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class DepartmentTabletAssetNumberPolicyForm(forms.Form):
    auto_enabled = forms.BooleanField(
        required=False,
        label="Automatically generate asset numbers",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    prefix = forms.CharField(
        max_length=128,
        required=False,
        label="Prefix",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    width = forms.IntegerField(
        min_value=1,
        max_value=20,
        label="Number width",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 20}),
    )

    def clean_prefix(self):
        return self.cleaned_data["prefix"].strip()

    def clean(self):
        cleaned_data = super().clean()
        prefix = cleaned_data.get("prefix", "")
        width = cleaned_data.get("width")
        if width is not None:
            asset_number_max_length = Tablet._meta.get_field("asset_number").max_length
            if len(prefix) + width > asset_number_max_length:
                self.add_error(
                    "prefix",
                    "The prefix and number width must fit within the Tablet asset-number length.",
                )
        return cleaned_data


class ApiVersionCompatibilityPolicyForm(forms.Form):
    minimum_app_version = forms.CharField(
        max_length=64,
        required=False,
        label="Minimum supported FireDash app version",
        help_text="Leave blank to allow all app versions using this API generation.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean_minimum_app_version(self):
        value = self.cleaned_data["minimum_app_version"].strip()
        if not value:
            return None
        try:
            return str(parse_app_version(value))
        except AppVersionError as error:
            raise forms.ValidationError(str(error)) from error


class VehicleRescueGuidesWebUrlForm(forms.Form):
    """The only editable field in the system-owned Euro RESCUE setting."""

    web_url = forms.CharField(
        max_length=2048,
        label="Web application URL",
        help_text="Must be an absolute HTTPS URL without embedded credentials.",
        widget=forms.URLInput(attrs={"class": "form-control", "inputmode": "url"}),
    )

    def clean_web_url(self):
        value = self.cleaned_data["web_url"]
        validate_vehicle_rescue_guides_web_url(value)
        return value


class SystemMailDeliveryModeForm(forms.Form):
    """Select the active system delivery mode without carrying credentials."""

    delivery_mode = forms.ChoiceField(
        choices=(
            (SystemMailConfiguration.DeliveryMode.DISABLED, "Disabled"),
            (SystemMailConfiguration.DeliveryMode.API, "Email API"),
            (SystemMailConfiguration.DeliveryMode.SMTP, "SMTP"),
        ),
        label="Active delivery mode",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    api_provider = forms.ChoiceField(
        required=False,
        label="API provider",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["api_provider"].choices = [
            (provider, provider.replace("_", " ").title()) for provider in API_PROVIDER_REGISTRY
        ]

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get(
            "delivery_mode"
        ) == SystemMailConfiguration.DeliveryMode.API and not cleaned_data.get("api_provider"):
            self.add_error("api_provider", "Select an API provider for Email API delivery.")
        return cleaned_data


class BrevoMailConfigurationForm(forms.Form):
    sender_name = forms.CharField(
        max_length=255, label="Sender name", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    sender_email = forms.EmailField(
        max_length=254,
        label="Sender email",
        widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"}),
    )


class ReplaceBrevoApiKeyForm(forms.Form):
    api_key = forms.CharField(
        label="API key",
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}, render_value=False
        ),
    )


class OutboundMailHttpsProxyForm(forms.Form):
    proxy_url = forms.CharField(
        required=False,
        max_length=2048,
        label="HTTPS egress proxy",
        help_text="Leave empty for direct HTTPS egress.",
        widget=forms.URLInput(attrs={"class": "form-control", "inputmode": "url"}),
    )

    def clean_proxy_url(self):
        value = self.cleaned_data["proxy_url"].strip()
        try:
            validate_outbound_mail_https_proxy(value)
        except OutboundMailTransportError:
            raise forms.ValidationError("Enter an HTTP(S) proxy URL with a hostname.") from None
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise forms.ValidationError(
                "Proxy URLs with embedded credentials are not supported here."
            )
        return value


class SmtpMailConfigurationForm(forms.Form):
    host = forms.CharField(
        max_length=255,
        label="SMTP host",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        label="Port",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    tls_mode = forms.ChoiceField(
        choices=SystemMailConfiguration.SmtpTlsMode.choices,
        label="TLS mode",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    sender_name = forms.CharField(
        max_length=255, label="Sender name", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    sender_email = forms.EmailField(
        max_length=254,
        label="Sender email",
        widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"}),
    )


class ReplaceSmtpCredentialsForm(forms.Form):
    username = forms.CharField(
        max_length=255, label="Username", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}, render_value=False
        ),
    )


class DepartmentMailDeliveryModeForm(forms.Form):
    """Department mail mode selection; service remains authoritative for eligibility."""

    delivery_mode = forms.ChoiceField(
        choices=(
            ("DISABLED", "Disabled"),
            ("SYSTEM", "FireDash managed service"),
            ("CUSTOM_SMTP", "Own SMTP server"),
        ),
        label="Outbound email mode",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class DepartmentRecipientPolicyForm(forms.Form):
    restriction_enabled = forms.BooleanField(
        required=False,
        label="Restrict recipients to approved domains",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    approved_domains = forms.CharField(
        required=False,
        label="Approved recipient domains",
        help_text=(
            "One exact domain per line, for example feuerwehr.hamburg.de. Do not include @; "
            "subdomains are not included."
        ),
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "spellcheck": "false"}),
    )

    def clean_approved_domains(self):
        # Domain canonicalization and validation deliberately remain in the
        # outbound-mail service, so all callers share its exact policy.
        return [domain for domain in self.cleaned_data["approved_domains"].splitlines() if domain]


class DepartmentOutboundEmailSettingsForm(forms.Form):
    delivery_mode = DepartmentMailDeliveryModeForm.base_fields["delivery_mode"]
    host = forms.CharField(required=False, max_length=255, label="Host", widget=forms.TextInput(attrs={"class": "form-control"}))
    port = forms.IntegerField(required=False, min_value=1, max_value=65535, label="Port", widget=forms.NumberInput(attrs={"class": "form-control"}))
    tls_mode = forms.ChoiceField(required=False, choices=(("", "Select TLS mode"), *SystemMailConfiguration.SmtpTlsMode.choices), label="TLS mode", widget=forms.Select(attrs={"class": "form-select"}))
    sender_name = forms.CharField(required=False, max_length=255, label="Sender name", widget=forms.TextInput(attrs={"class": "form-control"}))
    sender_email = forms.EmailField(required=False, max_length=254, label="Sender email", widget=forms.EmailInput(attrs={"class": "form-control"}))
    username = forms.CharField(required=False, max_length=255, label="Username", widget=forms.TextInput(attrs={"class": "form-control"}))
    password = forms.CharField(required=False, label="Password", widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}, render_value=False))
    approved_domains = forms.CharField(required=False, label="Approved recipient domains", help_text="One exact domain per line, for example feuerwehr.hamburg.de. Do not include @; subdomains are not included.", widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "spellcheck": "false"}))

    def clean_approved_domains(self):
        return [value for value in self.cleaned_data["approved_domains"].splitlines() if value]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("delivery_mode") == "CUSTOM_SMTP":
            for name in ("host", "port", "tls_mode", "sender_name", "sender_email"):
                if not cleaned.get(name):
                    self.add_error(name, "This field is required for Own SMTP server.")
            if cleaned.get("username") and not cleaned.get("password") and not self.initial.get("smtp_password_configured"):
                self.add_error("password", "A password is required when configuring SMTP authentication.")
        return cleaned
