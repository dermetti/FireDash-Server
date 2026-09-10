import base64
import json

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings

from apps.accounts.models import User
from apps.application_secrets.crypto import ApplicationSecretError, decrypt_application_secret
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.authorization.services import (
    grant_system_managed_mail_eligibility,
    revoke_system_managed_mail_eligibility,
)
from apps.organizations.models import Department
from apps.outbound_mail.models import DepartmentMailConfiguration
from apps.outbound_mail.services import (
    clear_department_smtp_credentials,
    configure_department_smtp,
    department_smtp_password_context,
    get_department_mail_configuration,
    get_department_recipient_policy,
    is_department_recipient_allowed,
    replace_department_smtp_credentials,
    set_department_delivery_mode,
    set_department_recipient_policy,
)


@pytest.fixture
def system_admin(db):
    actor = User.objects.create_user(email="system@example.test", display_name="System")
    SystemRole.objects.create(user=actor)
    return actor


@pytest.fixture
def departments(db, system_admin):
    first = Department.objects.create(name="First", short_code="first", created_by=system_admin)
    second = Department.objects.create(name="Second", short_code="second", created_by=system_admin)
    admin = User.objects.create_user(email="department@example.test", display_name="Department")
    DepartmentMembership.objects.create(user=admin, department=first, created_by=system_admin)
    return first, second, admin


@pytest.fixture
def application_secret_settings(tmp_path):
    keyring = tmp_path / "application-secret-kek-ring"
    keyring.write_text(json.dumps({"keys": {"1": base64.b64encode(b"a" * 32).decode("ascii")}}))
    with override_settings(
        APPLICATION_SECRET_KEK_CREDENTIAL_PATH=keyring, APPLICATION_SECRET_KEK_VERSION="1"
    ):
        yield


def _configure_smtp(*, actor, department):
    return configure_department_smtp(
        actor=actor,
        department=department,
        host="smtp.example.test",
        port=587,
        tls_mode="STARTTLS",
        sender_name="Department",
        sender_email="sender@example.test",
    )


def test_missing_configuration_is_disabled_and_switching_retains_smtp(
    system_admin, departments, application_secret_settings
):
    department, _, _ = departments
    assert get_department_mail_configuration(department=department).delivery_mode == "DISABLED"
    assert not DepartmentMailConfiguration.objects.filter(department=department).exists()

    _configure_smtp(actor=system_admin, department=department)
    replace_department_smtp_credentials(
        actor=system_admin, department=department, username="user", password="department-secret"
    )
    set_department_delivery_mode(
        actor=system_admin,
        department=department,
        delivery_mode=DepartmentMailConfiguration.DeliveryMode.CUSTOM_SMTP,
    )
    set_department_delivery_mode(
        actor=system_admin,
        department=department,
        delivery_mode=DepartmentMailConfiguration.DeliveryMode.DISABLED,
    )
    state = get_department_mail_configuration(department=department)
    assert state.delivery_mode == "DISABLED"
    assert state.smtp_host == "smtp.example.test"
    assert state.smtp_password_configured


def test_system_mode_requires_eligibility_and_revocation_preserves_configuration(
    system_admin, departments
):
    department, _, _ = departments
    with pytest.raises(ValidationError):
        set_department_delivery_mode(
            actor=system_admin,
            department=department,
            delivery_mode=DepartmentMailConfiguration.DeliveryMode.SYSTEM,
        )
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    set_department_delivery_mode(
        actor=system_admin,
        department=department,
        delivery_mode=DepartmentMailConfiguration.DeliveryMode.SYSTEM,
    )
    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)
    assert get_department_mail_configuration(department=department).delivery_mode == "SYSTEM"


def test_custom_smtp_activation_requires_complete_validated_transport(system_admin, departments):
    department, _, _ = departments
    with pytest.raises(ValidationError):
        set_department_delivery_mode(
            actor=system_admin,
            department=department,
            delivery_mode=DepartmentMailConfiguration.DeliveryMode.CUSTOM_SMTP,
        )
    with pytest.raises(ValidationError):
        configure_department_smtp(
            actor=system_admin,
            department=department,
            host="smtp.example.test",
            port=587,
            tls_mode="PLAINTEXT",
            sender_name="Department",
            sender_email="sender@example.test",
        )


def test_department_smtp_credentials_are_context_bound_and_write_only(
    system_admin, departments, application_secret_settings
):
    department, second, _ = departments
    configuration = replace_department_smtp_credentials(
        actor=system_admin, department=department, username="user", password="department-plaintext"
    )
    configuration.refresh_from_db()
    assert "department-plaintext" not in configuration.smtp_password_encrypted
    assert (
        decrypt_application_secret(
            configuration.smtp_password_encrypted,
            context=department_smtp_password_context(department=department),
        )
        == b"department-plaintext"
    )
    with pytest.raises(ApplicationSecretError):
        decrypt_application_secret(
            configuration.smtp_password_encrypted,
            context=department_smtp_password_context(department=second),
        )
    assert "department-plaintext" not in repr(configuration)
    clear_department_smtp_credentials(actor=system_admin, department=department)
    assert not get_department_mail_configuration(department=department).smtp_password_configured


def test_department_admin_is_limited_to_own_department_and_system_admin_may_support(
    system_admin, departments
):
    department, other, department_admin = departments
    _configure_smtp(actor=department_admin, department=department)
    with pytest.raises(PermissionDenied):
        _configure_smtp(actor=department_admin, department=other)
    _configure_smtp(actor=system_admin, department=other)


def test_recipient_policy_is_exact_normalized_and_defaults_to_unrestricted(
    system_admin, departments
):
    department, _, _ = departments
    assert is_department_recipient_allowed(
        department=department, recipient_email="any@elsewhere.test"
    )
    with pytest.raises(ValidationError):
        set_department_recipient_policy(
            actor=system_admin, department=department, restriction_enabled=True, approved_domains=[]
        )
    set_department_recipient_policy(
        actor=system_admin,
        department=department,
        restriction_enabled=True,
        approved_domains=["Feuerwehr.Hamburg.DE", "example.test"],
    )
    policy = get_department_recipient_policy(department=department)
    assert policy.approved_domains == ("example.test", "feuerwehr.hamburg.de")
    assert is_department_recipient_allowed(
        department=department, recipient_email="target@feuerwehr.hamburg.de"
    )
    assert not is_department_recipient_allowed(
        department=department, recipient_email="target@sub.feuerwehr.hamburg.de"
    )
    assert not is_department_recipient_allowed(
        department=department, recipient_email="target@notfeuerwehr.hamburg.de"
    )
    with pytest.raises(ValidationError):
        set_department_recipient_policy(
            actor=system_admin,
            department=department,
            restriction_enabled=True,
            approved_domains=["example.test", "EXAMPLE.TEST"],
        )
    for invalid in ("*.hamburg.de", ".hamburg.de", "192.0.2.1", "hamburg.de."):
        with pytest.raises(ValidationError):
            set_department_recipient_policy(
                actor=system_admin,
                department=department,
                restriction_enabled=True,
                approved_domains=[invalid],
            )


def test_department_mutations_audit_without_secrets_or_idempotent_noise(
    system_admin, departments, application_secret_settings
):
    department, _, _ = departments
    _configure_smtp(actor=system_admin, department=department)
    _configure_smtp(actor=system_admin, department=department)
    replace_department_smtp_credentials(
        actor=system_admin, department=department, username="user", password="audit-secret"
    )
    clear_department_smtp_credentials(actor=system_admin, department=department)
    clear_department_smtp_credentials(actor=system_admin, department=department)
    set_department_recipient_policy(
        actor=system_admin, department=department, restriction_enabled=False, approved_domains=[]
    )
    set_department_recipient_policy(
        actor=system_admin, department=department, restriction_enabled=False, approved_domains=[]
    )
    events = list(AuditEvent.objects.filter(department=department))
    actions = [event.action for event in events]
    assert actions.count("outbound_mail.department_smtp_configuration_changed") == 1
    assert actions.count("outbound_mail.department_smtp_credentials_cleared") == 1
    assert actions.count("outbound_mail.department_recipient_policy_changed") == 0
    assert "audit-secret" not in repr([(event.action, event.metadata) for event in events])
