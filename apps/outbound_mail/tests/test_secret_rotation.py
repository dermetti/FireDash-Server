import base64
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.application_secrets.crypto import (
    application_secret_key_version,
    decrypt_application_secret,
)
from apps.audit.models import AuditEvent
from apps.authorization.models import SystemRole
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration, SystemMailConfiguration
from apps.outbound_mail.secret_rotation import (
    OutboundMailSecretRotationError,
    outbound_mail_secret_rotation_status,
    rotate_outbound_mail_secrets,
)
from apps.outbound_mail.services import (
    BREVO_API_KEY_CONTEXT,
    SMTP_PASSWORD_CONTEXT,
    configure_brevo,
    department_smtp_password_context,
    replace_brevo_api_key,
    replace_department_smtp_credentials,
    replace_smtp_credentials,
)


@pytest.fixture
def system_admin(db):
    user = User.objects.create_user(email="admin@example.test", display_name="System")
    SystemRole.objects.create(user=user)
    return user


@pytest.fixture
def keyring(tmp_path):
    path = tmp_path / "application-secret-kek-ring"
    path.write_text(
        json.dumps(
            {
                "keys": {
                    "1": base64.b64encode(b"a" * 32).decode("ascii"),
                    "2": base64.b64encode(b"b" * 32).decode("ascii"),
                }
            }
        )
    )
    return path


@pytest.fixture
def old_key_settings(keyring):
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield keyring


def _old_credentials(*, actor):
    configure_brevo(actor=actor, sender_name="FireDash", sender_email="sender@example.test")
    replace_brevo_api_key(actor=actor, api_key="api-logical-secret")
    replace_smtp_credentials(actor=actor, username="system-user", password="system-logical-secret")
    department = Department.objects.create(name="Department", short_code="dept", created_by=actor)
    replace_department_smtp_credentials(
        actor=actor,
        department=department,
        username="department-user",
        password="department-logical-secret",
    )
    system = SystemMailConfiguration.objects.get(singleton=True)
    DepartmentMailConfiguration.objects.filter(department=department).update(
        last_smtp_verification_outcome="SUCCESS",
        last_smtp_verified_at=timezone.now(),
        last_smtp_verification_code="verified",
    )
    SystemMailConfiguration.objects.filter(pk=system.pk).update(
        verification_provider="BREVO",
        last_verification_outcome="SUCCESS",
        last_verified_at=timezone.now(),
        last_verification_code="verified",
    )
    system.refresh_from_db()
    department_configuration = DepartmentMailConfiguration.objects.get(department=department)
    return system, department, department_configuration


def test_rotation_reencrypts_all_mail_consumers_without_changing_mail_semantics(
    system_admin, old_key_settings
):
    system, department, department_configuration = _old_credentials(actor=system_admin)
    verification_before = (
        system.verification_provider,
        system.last_verification_outcome,
        system.last_verified_at,
        system.last_verification_code,
        department_configuration.last_smtp_verification_outcome,
        department_configuration.last_smtp_verified_at,
        department_configuration.last_smtp_verification_code,
    )
    audit_before = AuditEvent.objects.count()
    with override_settings(APPLICATION_SECRET_KEK_VERSION="2"):
        assert outbound_mail_secret_rotation_status().version_counts == {"1": 3}
        result = rotate_outbound_mail_secrets()
        assert result.rotated_count == 3
        system.refresh_from_db()
        department_configuration.refresh_from_db()
        assert application_secret_key_version(system.brevo_api_key_encrypted) == "2"
        assert application_secret_key_version(system.smtp_password_encrypted) == "2"
        assert (
            application_secret_key_version(department_configuration.smtp_password_encrypted) == "2"
        )
        assert (
            decrypt_application_secret(
                system.brevo_api_key_encrypted, context=BREVO_API_KEY_CONTEXT
            )
            == b"api-logical-secret"
        )
        assert (
            decrypt_application_secret(
                system.smtp_password_encrypted, context=SMTP_PASSWORD_CONTEXT
            )
            == b"system-logical-secret"
        )
        assert (
            decrypt_application_secret(
                department_configuration.smtp_password_encrypted,
                context=department_smtp_password_context(department=department),
            )
            == b"department-logical-secret"
        )
        assert outbound_mail_secret_rotation_status().all_live_credentials_active
        repeated = rotate_outbound_mail_secrets()
    assert repeated.rotated_count == 0 and repeated.skipped_active_count == 3
    assert verification_before == (
        system.verification_provider,
        system.last_verification_outcome,
        system.last_verified_at,
        system.last_verification_code,
        department_configuration.last_smtp_verification_outcome,
        department_configuration.last_smtp_verified_at,
        department_configuration.last_smtp_verification_code,
    )
    assert AuditEvent.objects.count() == audit_before


def test_status_and_retirement_check_fail_closed_for_live_or_malformed_envelopes(
    system_admin, old_key_settings, capsys
):
    system, _, _ = _old_credentials(actor=system_admin)
    with override_settings(APPLICATION_SECRET_KEK_VERSION="2"):
        status = outbound_mail_secret_rotation_status()
        assert not status.all_live_credentials_active
        assert not status.can_retire("1")
        with pytest.raises(CommandError):
            call_command("rotate_outbound_mail_secrets", "--check-retirement", "1")
        call_command("rotate_outbound_mail_secrets", "--status")
        assert "api-logical-secret" not in capsys.readouterr().out
        system.brevo_api_key_encrypted = "malformed-envelope"
        system.save(update_fields=("brevo_api_key_encrypted",))
        assert not outbound_mail_secret_rotation_status().can_retire("unused-version")


def test_rotation_stops_safely_and_is_rerunnable_after_malformed_or_missing_old_key(
    system_admin, old_key_settings, keyring
):
    system, _, _ = _old_credentials(actor=system_admin)
    original_smtp_envelope = system.smtp_password_encrypted
    system.smtp_password_encrypted = "malformed-envelope"
    system.save(update_fields=("smtp_password_encrypted",))
    with override_settings(APPLICATION_SECRET_KEK_VERSION="2"):
        with pytest.raises(OutboundMailSecretRotationError) as raised:
            rotate_outbound_mail_secrets()
        assert raised.value.reference.startswith("system_smtp_password:")
        assert "logical-secret" not in str(raised.value)
        assert "malformed-envelope" not in str(raised.value)
        system.refresh_from_db()
        assert application_secret_key_version(system.brevo_api_key_encrypted) == "2"
        system.smtp_password_encrypted = original_smtp_envelope
        system.save(update_fields=("smtp_password_encrypted",))
        assert rotate_outbound_mail_secrets().rotated_count == 2

    keyring.write_text(json.dumps({"keys": {"2": base64.b64encode(b"b" * 32).decode("ascii")}}))
    system.smtp_password_encrypted = original_smtp_envelope
    system.save(update_fields=("smtp_password_encrypted",))
    with override_settings(APPLICATION_SECRET_KEK_VERSION="2"):
        with pytest.raises(OutboundMailSecretRotationError) as raised:
            rotate_outbound_mail_secrets()
    assert raised.value.code == "unknown_key_version"
    assert "system-logical-secret" not in str(raised.value)
