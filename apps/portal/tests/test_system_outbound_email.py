"""System-admin presentation coverage for outbound-email configuration."""

import base64
import json
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import SystemRole
from apps.authorization.services import is_system_managed_mail_allowed
from apps.organizations.models import Department
from apps.outbound_mail.models import SystemMailConfiguration
from apps.outbound_mail.runtime import ProviderUnavailableError


@pytest.fixture
def system_admin(db):
    user = User.objects.create_user("system-mail@example.test", "System mail", "safe-password")
    SystemRole.objects.create(user=user)
    return user


@pytest.fixture
def non_system_admin(db):
    return User.objects.create_user("not-system@example.test", "Not system", "safe-password")


@pytest.fixture
def application_secret_settings(tmp_path):
    keyring = tmp_path / "application-secret-kek-ring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"a" * 32).decode("ascii")}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield


def _reauthenticate(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


@pytest.mark.django_db
def test_outbound_email_is_system_admin_only_and_is_listed_in_system_data_hub(
    client, system_admin, non_system_admin
):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    response = client.get(url)
    assert response.status_code == 200
    assert "Outbound Email" in response.content.decode()
    hub = client.get(reverse("portal-system-data-hub"))
    assert url in hub.content.decode()

    client.force_login(non_system_admin)
    assert client.get(url).status_code == 403
    assert client.post(url, {"action": "brevo_clear"}).status_code == 403


@pytest.mark.django_db
def test_api_and_smtp_configuration_use_services_and_retain_inactive_settings(
    client, system_admin, application_secret_settings
):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    _reauthenticate(client)
    assert (
        client.post(
            url,
            {
                "action": "brevo_configuration",
                "sender_name": "FireDash",
                "sender_email": "api@example.test",
            },
        ).status_code
        == 302
    )
    assert (
        client.post(url, {"action": "brevo_key", "api_key": "api-secret-value"}).status_code == 302
    )
    assert (
        client.post(
            url, {"action": "mode", "delivery_mode": "API", "api_provider": "BREVO"}
        ).status_code
        == 302
    )

    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.API
    assert configuration.brevo_api_key_configured
    assert configuration.brevo_api_key_encrypted != "api-secret-value"

    assert (
        client.post(
            url,
            {
                "action": "smtp_configuration",
                "host": "smtp.example.test",
                "port": 587,
                "tls_mode": "STARTTLS",
                "sender_name": "FireDash SMTP",
                "sender_email": "smtp@example.test",
            },
        ).status_code
        == 302
    )
    assert (
        client.post(
            url,
            {
                "action": "smtp_credentials",
                "username": "smtp-user",
                "password": "smtp-secret-value",
            },
        ).status_code
        == 302
    )
    assert (
        client.post(
            url, {"action": "mode", "delivery_mode": "SMTP", "api_provider": ""}
        ).status_code
        == 302
    )
    assert (
        client.post(
            url, {"action": "mode", "delivery_mode": "DISABLED", "api_provider": ""}
        ).status_code
        == 302
    )

    configuration.refresh_from_db()
    assert configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.DISABLED
    assert configuration.brevo_api_key_configured and configuration.smtp_password_configured
    assert AuditEvent.objects.filter(action="outbound_mail.delivery_mode_changed").count() == 3


@pytest.mark.django_db
def test_credentials_are_write_only_and_replace_clear_remain_audited(
    client, system_admin, application_secret_settings
):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    _reauthenticate(client)
    client.post(url, {"action": "brevo_key", "api_key": "never-render-this-api-key"})
    client.post(
        url,
        {
            "action": "smtp_credentials",
            "username": "smtp-user",
            "password": "never-render-this-password",
        },
    )
    content = client.get(url).content.decode()
    assert "never-render-this-api-key" not in content
    assert "never-render-this-password" not in content
    assert "app-secret:" not in content
    assert "Configured" in content

    assert client.post(url, {"action": "brevo_clear"}).status_code == 302
    assert client.post(url, {"action": "smtp_clear"}).status_code == 302
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert not configuration.brevo_api_key_configured
    assert not configuration.smtp_password_configured
    assert AuditEvent.objects.filter(action="outbound_mail.brevo_api_key_cleared").exists()
    assert AuditEvent.objects.filter(action="outbound_mail.smtp_credentials_cleared").exists()


@pytest.mark.django_db
def test_verification_is_non_delivery_service_action_and_presents_sanitized_status(
    client, system_admin
):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    _reauthenticate(client)
    configuration, _ = SystemMailConfiguration.objects.get_or_create(singleton=True)
    configuration.delivery_mode = SystemMailConfiguration.DeliveryMode.SMTP
    configuration.save(update_fields=("delivery_mode",))

    class UnavailableProvider:
        provider_id = "SMTP"

        def verify(self):
            raise ProviderUnavailableError()

    with patch(
        "apps.outbound_mail.services.resolve_effective_provider",
        return_value=UnavailableProvider(),
    ) as resolve:
        response = client.post(url, {"action": "verify"})
    assert response.status_code == 302
    resolve.assert_called_once_with(delivery_mode="SMTP", api_provider="")
    configuration.refresh_from_db()
    assert configuration.delivery_mode == SystemMailConfiguration.DeliveryMode.SMTP
    assert configuration.last_verification_outcome == "FAILED"
    assert configuration.last_verification_code == "provider_unavailable"
    content = client.get(url).content.decode()
    assert "Did not succeed" in content and "provider_unavailable" in content
    assert "raw provider response" not in content


@pytest.mark.django_db
def test_invalid_or_incomplete_configuration_is_safe_form_feedback(client, system_admin):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    _reauthenticate(client)
    response = client.post(
        url,
        {"action": "mode", "delivery_mode": "API", "api_provider": "BREVO"},
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert "incomplete" in content.lower()
    assert "app-secret:" not in content
    assert "api-key" not in content.lower()


@pytest.mark.django_db
def test_system_admin_ui_grants_and_revokes_department_managed_mail_access(
    client, system_admin, non_system_admin
):
    url = reverse("portal-system-outbound-email")
    department = Department.objects.create(
        name="Department Mail", short_code="DML", created_by=system_admin
    )
    client.force_login(system_admin)
    content = client.get(url).content.decode()
    assert "Department access to system-managed email" in content
    assert "Department Mail" in content and "Not allowed" in content
    _reauthenticate(client)
    assert (
        client.post(
            url,
            {
                "action": "department_mail_eligibility",
                "department_id": department.id,
                "allowed": "grant",
            },
        ).status_code
        == 302
    )
    assert is_system_managed_mail_allowed(department=department)
    assert (
        AuditEvent.objects.filter(
            action="authorization.department_managed_mail_eligibility_granted",
            department=department,
        ).count()
        == 1
    )
    assert (
        client.post(
            url,
            {
                "action": "department_mail_eligibility",
                "department_id": department.id,
                "allowed": "revoke",
            },
        ).status_code
        == 302
    )
    assert not is_system_managed_mail_allowed(department=department)

    client.force_login(non_system_admin)
    assert (
        client.post(
            url,
            {
                "action": "department_mail_eligibility",
                "department_id": department.id,
                "allowed": "grant",
            },
        ).status_code
        == 403
    )
