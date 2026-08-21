"""Stage 1 authentication shell tests: vendored assets, no CDN, Bootstrap forms."""

import pytest
from django.test import Client

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


@pytest.mark.django_db
def test_reauthenticate_page_uses_bootstrap_form(client):
    user = User.objects.create_user("reauth@example.test", "Reauth", "safe-password")
    client.force_login(user)
    content = client.get("/accounts/reauthenticate/").content.decode()
    assert "vendor/bootstrap/bootstrap.min.css" in content
    assert "form-control" in content
