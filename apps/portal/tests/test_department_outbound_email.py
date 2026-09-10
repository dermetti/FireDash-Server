import base64
import json

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.authorization.services import (
    grant_system_managed_mail_eligibility,
    revoke_system_managed_mail_eligibility,
)
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration


@pytest.fixture
def scope(db):
    system_admin = User.objects.create_user("system@example.test", "System", "safe-password")
    SystemRole.objects.create(user=system_admin)
    department = Department.objects.create(name="Own", short_code="OWN", created_by=system_admin)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=system_admin)
    administrator = User.objects.create_user(
        "department@example.test", "Department", "safe-password"
    )
    DepartmentMembership.objects.create(
        user=administrator, department=department, created_by=system_admin
    )
    return system_admin, administrator, department, other


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


def _url(department):
    return reverse("portal-department-outbound-email", args=(department.id,))


@pytest.mark.django_db
def test_department_admin_scope_and_missing_configuration_are_fail_closed(client, scope):
    _system_admin, administrator, department, other = scope
    client.force_login(administrator)
    response = client.get(_url(department))
    assert response.status_code == 200
    assert "Disabled" in response.content.decode()
    assert "Brevo" not in response.content.decode()
    assert "proxy" not in response.content.decode().lower()
    assert not DepartmentMailConfiguration.objects.filter(department=department).exists()
    assert client.get(_url(other)).status_code == 403
    assert (
        client.post(_url(other), {"action": "mode", "delivery_mode": "DISABLED"}).status_code == 403
    )


@pytest.mark.django_db
def test_system_mode_availability_and_revocation_are_presented_without_mutation(client, scope):
    system_admin, administrator, department, _ = scope
    client.force_login(administrator)
    _reauthenticate(client)
    response = client.post(_url(department), {"action": "mode", "delivery_mode": "SYSTEM"})
    assert response.status_code == 200
    assert "not authorized" in response.content.decode()
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    assert (
        client.post(_url(department), {"action": "mode", "delivery_mode": "SYSTEM"}).status_code
        == 302
    )
    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)
    content = client.get(_url(department)).content.decode()
    assert "managed service is unavailable" in content
    assert DepartmentMailConfiguration.objects.get(department=department).delivery_mode == "SYSTEM"
    assert (
        client.post(_url(department), {"action": "mode", "delivery_mode": "DISABLED"}).status_code
        == 302
    )


@pytest.mark.django_db
def test_smtp_credentials_are_write_only_and_recipient_policy_uses_services(
    client, scope, application_secret_settings
):
    _system_admin, administrator, department, _ = scope
    client.force_login(administrator)
    _reauthenticate(client)
    assert (
        client.post(
            _url(department),
            {
                "action": "smtp_configuration",
                "host": "smtp.example.test",
                "port": 587,
                "tls_mode": "STARTTLS",
                "sender_name": "Own",
                "sender_email": "sender@example.test",
            },
        ).status_code
        == 302
    )
    assert (
        client.post(
            _url(department),
            {"action": "smtp_credentials", "username": "user", "password": "never-render-password"},
        ).status_code
        == 302
    )
    content = client.get(_url(department)).content.decode()
    configuration = DepartmentMailConfiguration.objects.get(department=department)
    assert "never-render-password" not in content
    assert configuration.smtp_password_encrypted not in content
    assert "app-secret:" not in content
    assert client.post(_url(department), {"action": "smtp_clear"}).status_code == 302
    assert not DepartmentMailConfiguration.objects.get(
        department=department
    ).smtp_password_configured

    assert (
        client.post(
            _url(department),
            {
                "action": "recipient_policy",
                "restriction_enabled": "on",
                "approved_domains": "feuerwehr.hamburg.de\nexample.test",
            },
        ).status_code
        == 302
    )
    content = client.get(_url(department)).content.decode()
    assert "subdomains are not included" in content
    invalid = client.post(
        _url(department),
        {
            "action": "recipient_policy",
            "restriction_enabled": "on",
            "approved_domains": "*.example.test",
        },
    )
    assert invalid.status_code == 200
    assert "valid exact domain names" in invalid.content.decode()
    assert AuditEvent.objects.filter(
        action="outbound_mail.department_recipient_policy_changed"
    ).exists()
