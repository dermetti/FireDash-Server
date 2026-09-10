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
from apps.outbound_mail.http_transport import OutboundMailHttpTransport
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
    assert "••••••••••••" in content
    assert 'value="••••••••••••"' not in content
    assert 'name="api_key" value=' not in content
    assert 'name="password" value=' not in content

    assert client.post(url, {"action": "brevo_clear"}).status_code == 302
    assert client.post(url, {"action": "smtp_clear"}).status_code == 302
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert not configuration.brevo_api_key_configured
    assert not configuration.smtp_password_configured
    assert AuditEvent.objects.filter(action="outbound_mail.brevo_api_key_cleared").exists()
    assert AuditEvent.objects.filter(action="outbound_mail.smtp_credentials_cleared").exists()


@pytest.mark.django_db
def test_system_admin_can_configure_the_https_egress_proxy(client, system_admin):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    _reauthenticate(client)
    response = client.post(
        url,
        {"action": "https_proxy", "proxy_url": "http://proxy.example.test:3128"},
    )
    assert response.status_code == 302
    configuration = SystemMailConfiguration.objects.get(singleton=True)
    assert configuration.outbound_mail_https_proxy == "http://proxy.example.test:3128"
    assert AuditEvent.objects.filter(
        action="outbound_mail.https_proxy_changed",
        metadata__configured=True,
    ).exists()

    transport = OutboundMailHttpTransport()
    assert transport._proxies == {"https": "http://proxy.example.test:3128"}

    response = client.post(url, {"action": "https_proxy", "proxy_url": "socks5://bad.example.test"})
    assert response.status_code == 200
    assert "HTTP(S) proxy URL" in response.content.decode()
    configuration.refresh_from_db()
    assert configuration.outbound_mail_https_proxy == "http://proxy.example.test:3128"


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

    response = client.post(url, {"action": "smtp_configuration", "host": ""})
    content = response.content.decode()
    assert response.status_code == 200
    assert "SMTP configuration" in content
    assert "API key:" not in content


@pytest.mark.django_db
def test_managed_mail_eligibility_is_on_the_system_department_detail(
    client, system_admin, non_system_admin
):
    department = Department.objects.create(
        name="Department Mail", short_code="DML", created_by=system_admin
    )
    outbound_url = reverse("portal-system-outbound-email")
    detail_url = reverse("portal-system-department", args=(department.id,))
    client.force_login(system_admin)
    content = client.get(outbound_url).content.decode()
    assert "Department access to system-managed email" not in content
    assert "Department Mail" not in content
    content = client.get(detail_url).content.decode()
    assert "FireDash managed outbound email" in content
    assert "does not enable department email" in content
    assert "Not allowed" in content
    _reauthenticate(client)
    assert (
        client.post(
            detail_url,
            {
                "action": "managed-mail-eligibility",
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
            detail_url,
            {
                "action": "managed-mail-eligibility",
                "allowed": "revoke",
            },
        ).status_code
        == 302
    )
    assert not is_system_managed_mail_allowed(department=department)

    client.force_login(non_system_admin)
    assert (
        client.post(
            detail_url,
            {
                "action": "managed-mail-eligibility",
                "allowed": "grant",
            },
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_settings_card_adapts_with_htmx_and_cards_are_stacked(client, system_admin):
    url = reverse("portal-system-outbound-email")
    client.force_login(system_admin)
    content = client.get(url).content.decode()
    assert 'id="system-mail-settings"' in content
    assert "Department access to system-managed email" not in content
    assert "Email API configuration" not in content
    assert "SMTP configuration" not in content
    assert "row g-4" not in content
    assert "app-desktop-sidebar" in content
    assert "position: fixed" in content
    assert "d-lg-none" in content

    smtp = client.get(url, {"settings_method": "SMTP"}, HTTP_HX_REQUEST="true")
    assert smtp.status_code == 200
    smtp_content = smtp.content.decode()
    assert 'id="system-mail-settings"' in smtp_content
    assert "SMTP configuration" in smtp_content
    assert "API key:" not in smtp_content
    assert "<!doctype" not in smtp_content.lower()

    api = client.get(url, {"settings_method": "API"}, HTTP_HX_REQUEST="true")
    assert api.status_code == 200
    api_content = api.content.decode()
    assert "API key:" in api_content
    assert "SMTP password:" not in api_content
