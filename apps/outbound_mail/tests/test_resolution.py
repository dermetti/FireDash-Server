import base64
import json
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import SystemRole
from apps.authorization.services import (
    grant_system_managed_mail_eligibility,
    revoke_system_managed_mail_eligibility,
)
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration, SystemMailConfiguration
from apps.outbound_mail.providers import BREVO, RUNTIME_PROVIDER_REGISTRY
from apps.outbound_mail.resolution import (
    OutboundMailUnavailableReason,
    resolve_department_mail_provider,
)
from apps.outbound_mail.runtime import VerificationOutcome
from apps.outbound_mail.services import (
    configure_brevo,
    configure_department_smtp,
    configure_smtp,
    replace_brevo_api_key,
    replace_department_smtp_credentials,
    set_delivery_mode,
    set_department_delivery_mode,
)


@pytest.fixture
def system_admin(db):
    user = User.objects.create_user(email="admin@example.test", display_name="System")
    SystemRole.objects.create(user=user)
    return user


@pytest.fixture
def department(system_admin):
    return Department.objects.create(name="Department", short_code="dept", created_by=system_admin)


@pytest.fixture
def application_secret_settings(tmp_path):
    keyring = tmp_path / "application-secret-kek-ring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"a" * 32).decode("ascii")}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield


def _configure_department_smtp(*, actor, department):
    configure_department_smtp(
        actor=actor,
        department=department,
        host="smtp.department.test",
        port=587,
        tls_mode="STARTTLS",
        sender_name="Department",
        sender_email="sender@department.test",
    )
    replace_department_smtp_credentials(
        actor=actor, department=department, username="smtp-user", password="department-secret"
    )


def _mark_department_smtp_verified(*, department):
    DepartmentMailConfiguration.objects.filter(department=department).update(
        last_smtp_verification_outcome=VerificationOutcome.SUCCESS,
        last_smtp_verified_at=timezone.now(),
        last_smtp_verification_code="verified",
    )


def _configure_verified_system_api(*, actor):
    configure_brevo(actor=actor, sender_name="FireDash", sender_email="sender@example.test")
    replace_brevo_api_key(actor=actor, api_key="api-secret")
    set_delivery_mode(actor=actor, delivery_mode="API", api_provider=BREVO)
    SystemMailConfiguration.objects.filter(singleton=True).update(
        verification_provider=BREVO,
        last_verification_outcome=VerificationOutcome.SUCCESS,
        last_verified_at=timezone.now(),
        last_verification_code="verified",
    )


def test_missing_or_disabled_department_is_unavailable_without_creating_state(
    system_admin, department
):
    events_before = AuditEvent.objects.count()
    result = resolve_department_mail_provider(department=department)
    assert not result.is_usable
    assert result.unavailable_reason == OutboundMailUnavailableReason.DEPARTMENT_DISABLED
    assert not DepartmentMailConfiguration.objects.filter(department=department).exists()
    assert AuditEvent.objects.count() == events_before


def test_system_resolution_requires_eligibility_complete_current_verified_configuration(
    system_admin, department, application_secret_settings
):
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    set_department_delivery_mode(actor=system_admin, department=department, delivery_mode="SYSTEM")

    assert (
        resolve_department_mail_provider(department=department).unavailable_reason
        == OutboundMailUnavailableReason.SYSTEM_MAIL_DISABLED
    )
    _configure_verified_system_api(actor=system_admin)
    SystemMailConfiguration.objects.filter(singleton=True).update(
        last_verification_outcome="FAILED"
    )
    with patch("apps.outbound_mail.resolution.resolve_effective_provider") as resolve_provider:
        result = resolve_department_mail_provider(department=department)
    assert result.unavailable_reason == OutboundMailUnavailableReason.SYSTEM_VERIFICATION_REQUIRED
    resolve_provider.assert_not_called()
    SystemMailConfiguration.objects.filter(singleton=True).update(
        last_verification_outcome=VerificationOutcome.SUCCESS
    )
    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)
    result = resolve_department_mail_provider(department=department)
    assert result.unavailable_reason == OutboundMailUnavailableReason.MANAGED_MAIL_NOT_ALLOWED
    assert DepartmentMailConfiguration.objects.get(department=department).delivery_mode == "SYSTEM"


def test_system_api_uses_generic_registry_only_after_verified_readiness(
    system_admin, department, application_secret_settings
):
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    set_department_delivery_mode(actor=system_admin, department=department, delivery_mode="SYSTEM")
    _configure_verified_system_api(actor=system_admin)

    class FakeProvider:
        provider_id = BREVO

    RUNTIME_PROVIDER_REGISTRY[BREVO] = FakeProvider()
    try:
        result = resolve_department_mail_provider(department=department)
    finally:
        RUNTIME_PROVIDER_REGISTRY.pop(BREVO, None)
    assert result.is_usable
    assert result.provider_id == BREVO
    assert isinstance(result.provider, FakeProvider)
    assert "api-secret" not in repr(result)


def test_system_smtp_uses_the_existing_runtime_provider_boundary(system_admin, department):
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    set_department_delivery_mode(actor=system_admin, department=department, delivery_mode="SYSTEM")
    configure_smtp(
        actor=system_admin,
        host="smtp.system.test",
        port=465,
        tls_mode="IMPLICIT_TLS",
        sender_name="FireDash",
        sender_email="sender@example.test",
    )
    set_delivery_mode(actor=system_admin, delivery_mode="SMTP")
    SystemMailConfiguration.objects.filter(singleton=True).update(
        verification_provider="SMTP",
        last_verification_outcome=VerificationOutcome.SUCCESS,
        last_verified_at=timezone.now(),
        last_verification_code="verified",
    )
    result = resolve_department_mail_provider(department=department)
    assert result.is_usable and result.provider_id == "SMTP"
    assert result.provider.__class__.__name__ == "SmtpProvider"


def test_custom_smtp_requires_complete_current_verified_configuration_and_does_not_decrypt_early(
    system_admin, department, application_secret_settings
):
    set_department_delivery_mode(
        actor=system_admin, department=department, delivery_mode="DISABLED"
    )
    _configure_department_smtp(actor=system_admin, department=department)
    set_department_delivery_mode(
        actor=system_admin, department=department, delivery_mode="CUSTOM_SMTP"
    )

    with patch("apps.outbound_mail.resolution.decrypt_application_secret") as decrypt:
        result = resolve_department_mail_provider(department=department)
    assert (
        result.unavailable_reason
        == OutboundMailUnavailableReason.DEPARTMENT_SMTP_VERIFICATION_REQUIRED
    )
    decrypt.assert_not_called()

    _mark_department_smtp_verified(department=department)
    result = resolve_department_mail_provider(department=department)
    assert result.is_usable and result.provider_id == "SMTP"
    assert "department-secret" not in repr(result)

    DepartmentMailConfiguration.objects.filter(department=department).update(smtp_host="")
    with patch("apps.outbound_mail.resolution.decrypt_application_secret") as decrypt:
        result = resolve_department_mail_provider(department=department)
    assert (
        result.unavailable_reason
        == OutboundMailUnavailableReason.DEPARTMENT_SMTP_CONFIGURATION_INCOMPLETE
    )
    decrypt.assert_not_called()


def test_resolution_is_read_only_and_sanitizes_bad_credentials(
    system_admin, department, application_secret_settings
):
    _configure_department_smtp(actor=system_admin, department=department)
    set_department_delivery_mode(
        actor=system_admin, department=department, delivery_mode="CUSTOM_SMTP"
    )
    _mark_department_smtp_verified(department=department)
    configuration = DepartmentMailConfiguration.objects.get(department=department)
    configuration.smtp_password_encrypted = "malformed-envelope"
    configuration.save(update_fields=("smtp_password_encrypted",))
    state_before = (
        configuration.delivery_mode,
        configuration.last_smtp_verification_outcome,
        configuration.last_smtp_verified_at,
    )
    events_before = AuditEvent.objects.count()
    result = resolve_department_mail_provider(department=department)
    configuration.refresh_from_db()
    assert result.unavailable_reason == OutboundMailUnavailableReason.PROVIDER_CONFIGURATION
    assert "malformed-envelope" not in repr(result)
    assert state_before == (
        configuration.delivery_mode,
        configuration.last_smtp_verification_outcome,
        configuration.last_smtp_verified_at,
    )
    assert AuditEvent.objects.count() == events_before
