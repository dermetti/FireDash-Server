from django import forms
from django.contrib.auth.password_validation import validate_password


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=254)
    password = forms.CharField(widget=forms.PasswordInput)


class TokenForm(forms.Form):
    token = forms.CharField(min_length=6, max_length=12)


class SetupForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput)
    password_confirm = forms.CharField(widget=forms.PasswordInput)

    def clean(self):
        cleaned_data = super().clean() or {}
        password = cleaned_data.get("password")
        if password and password == cleaned_data.get("password_confirm"):
            validate_password(password)
        elif password:
            self.add_error("password_confirm", "Passwords do not match.")
        return cleaned_data


class ReauthenticationForm(forms.Form):
    token = forms.CharField(min_length=6, max_length=12, label="TOTP Code")
