import base64
import json
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.authorization.services import grant_system_managed_mail_eligibility
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration
from apps.outbound_mail.runtime import ProviderUnavailableError


@pytest.fixture
def scope(db):
    system = User.objects.create_user("system@example.test", "System", "safe-password")
    SystemRole.objects.create(user=system)
    department = Department.objects.create(name="Own", short_code="OWN", created_by=system)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=system)
    admin = User.objects.create_user("department@example.test", "Department", "safe-password")
    DepartmentMembership.objects.create(user=admin, department=department, created_by=system)
    return system, admin, department, other


@pytest.fixture
def secret_settings(tmp_path):
    keyring = tmp_path / "keyring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"a" * 32).decode()}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield


def _reauth(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


def _url(department):
    return reverse("portal-department-settings", args=(department.id,))


def _smtp_payload(**changes):
    payload = {
        "action": "email-settings",
        "delivery_mode": "CUSTOM_SMTP",
        "host": "smtp.example.test",
        "port": 587,
        "tls_mode": "STARTTLS",
        "sender_name": "Own",
        "sender_email": "sender@example.test",
        "username": "user",
        "password": "write-only-secret",
        "approved_domains": "example.test",
    }
    payload.update(changes)
    return payload


@pytest.mark.django_db
def test_email_is_one_settings_card_not_navigation_and_mode_is_adaptive(client, scope):
    system, admin, department, other = scope
    client.force_login(admin)
    content = client.get(_url(department)).content.decode()
    assert 'id="department-outbound-email"' in content
    assert (
        "Outbound Email"
        not in content.split('aria-label="Primary navigation"', 1)[1].split("</nav>", 1)[0]
    )
    assert "SMTP password" not in content
    assert "FireDash managed service not available" in content
    partial = client.get(_url(department), {"delivery_mode": "CUSTOM_SMTP"}, HTTP_HX_REQUEST="true")
    assert partial.status_code == 200 and "Own SMTP server" in partial.content.decode()
    assert client.get(_url(other)).status_code == 403
    grant_system_managed_mail_eligibility(actor=system, department=department)
    assert "available" in client.get(_url(department)).content.decode()


@pytest.mark.django_db
def test_one_apply_preserves_blank_password_sets_domains_and_verifies(
    client, scope, secret_settings
):
    _system, admin, department, _other = scope
    client.force_login(admin)
    _reauth(client)
    with patch(
        "apps.outbound_mail.smtp.SmtpProvider.verify", side_effect=ProviderUnavailableError()
    ):
        response = client.post(_url(department), _smtp_payload())
    assert response.status_code == 302
    config = DepartmentMailConfiguration.objects.get(department=department)
    encrypted = config.smtp_password_encrypted
    assert config.delivery_mode == "CUSTOM_SMTP"
    assert config.last_smtp_verification_outcome == "FAILED"
    response = client.post(_url(department), _smtp_payload(password="", approved_domains=""))
    assert response.status_code == 302
    config.refresh_from_db()
    assert config.smtp_password_encrypted == encrypted
    content = client.get(_url(department)).content.decode()
    assert "write-only-secret" not in content and encrypted not in content
    assert "Restrict recipients to approved domains" not in content
    assert "Save SMTP configuration" not in content and "Verify SMTP configuration" not in content
