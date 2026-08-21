"""Stage 1 authentication shell tests: vendored assets, no CDN, Bootstrap forms."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User


def test_login_page_uses_vendored_bootstrap_without_cdn():
    content = Client().get("/accounts/login/").content.decode()
    assert "vendor/bootstrap/bootstrap.min.css" in content
    assert "cdn.jsdelivr" not in content
    assert "form-control" in content


def test_setup_page_uses_vendored_bootstrap_without_cdn():
    content = Client().get("/accounts/setup/a-valid-looking-token/").content.decode()
    assert "vendor/bootstrap/bootstrap.min.css" in content
    assert "cdn.jsdelivr" not in content
    assert "form-control" in content
    assert "form-label" in content


def test_login_form_has_no_htmx_submission_attributes():
    content = Client().get("/accounts/login/").content.decode()
    assert "hx-post" not in content
    assert "hx-target" not in content
    assert "hx-swap" not in content


@pytest.mark.django_db
def test_invalid_login_returns_single_complete_page_with_one_error(client):
    response = client.post(
        reverse("accounts-login"), {"email": "nobody@example.test", "password": "wrong-password"}
    )
    content = response.content.decode()
    assert response.status_code == 200
    # Exactly one HTML document: no nested auth shell was swapped in.
    assert content.count("<html") == 1
    assert content.count("Invalid credentials.") == 1


@pytest.mark.django_db
def test_overview_uses_authenticated_shell_not_auth_card(client):
    user = User.objects.create_user("overview@example.test", "Overview", "safe-password")
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    content = response.content.decode()
    assert response.status_code == 200
    assert 'id="navOffcanvas"' in content
    assert 'id="authentication-card"' not in content


@pytest.mark.django_db
def test_reauthenticate_page_uses_bootstrap_form(client):
    user = User.objects.create_user("reauth@example.test", "Reauth", "safe-password")
    client.force_login(user)
    content = client.get("/accounts/reauthenticate/").content.decode()
    assert "vendor/bootstrap/bootstrap.min.css" in content
    assert "form-control" in content
