from django import forms
from django.contrib.auth.password_validation import validate_password


class LoginForm(forms.Form):
    email = forms.EmailField(
        max_length=254, widget=forms.EmailInput(attrs={"class": "form-control"})
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs={"class": "form-control"}))


class TokenForm(forms.Form):
    token = forms.CharField(
        min_length=6, max_length=12, widget=forms.TextInput(attrs={"class": "form-control"})
    )


class SetupForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput(attrs={"class": "form-control"}))
    password_confirm = forms.CharField(widget=forms.PasswordInput(attrs={"class": "form-control"}))

    def clean(self):
        cleaned_data = super().clean() or {}
        password = cleaned_data.get("password")
        if password and password == cleaned_data.get("password_confirm"):
            validate_password(password)
        elif password:
            self.add_error("password_confirm", "Passwords do not match.")
        return cleaned_data


class ReauthenticationForm(forms.Form):
    token = forms.CharField(
        min_length=6,
        max_length=12,
        label="TOTP Code",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "one-time-code"}),
    )
