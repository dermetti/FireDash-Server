"""Fail-closed system-managed outbound-mail authorization coverage."""

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.authorization.services import (
    grant_system_managed_mail_eligibility,
    is_system_managed_mail_allowed,
    revoke_system_managed_mail_eligibility,
)
from apps.organizations.models import Department
from apps.outbound_mail.models import SystemMailConfiguration


@pytest.fixture
def eligibility_scope(db):
    system_admin = User.objects.create_user("system@example.test", "System", "safe-password")
    department_admin = User.objects.create_user(
        "department@example.test", "Department", "safe-password"
    )
    SystemRole.objects.create(user=system_admin)
    department = Department.objects.create(
        name="Eligible Department", short_code="ELG", created_by=system_admin
    )
    DepartmentMembership.objects.create(
        user=department_admin, department=department, created_by=system_admin
    )
    return system_admin, department_admin, department


@pytest.mark.django_db
def test_managed_mail_eligibility_is_missing_state_denied_and_explicit_grant_only(
    eligibility_scope,
):
    system_admin, _, department = eligibility_scope
    assert not is_system_managed_mail_allowed(department=department)

    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    assert is_system_managed_mail_allowed(department=department)

    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)
    assert not is_system_managed_mail_allowed(department=department)


@pytest.mark.django_db
def test_only_system_admin_may_change_managed_mail_eligibility(eligibility_scope):
    _, department_admin, department = eligibility_scope
    with pytest.raises(PermissionDenied):
        grant_system_managed_mail_eligibility(actor=department_admin, department=department)
    with pytest.raises(PermissionDenied):
        revoke_system_managed_mail_eligibility(actor=department_admin, department=department)
    assert not is_system_managed_mail_allowed(department=department)


@pytest.mark.django_db
def test_eligibility_audits_changes_but_not_idempotent_requests(eligibility_scope):
    system_admin, _, department = eligibility_scope
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)
    revoke_system_managed_mail_eligibility(actor=system_admin, department=department)

    events = AuditEvent.objects.filter(department=department).order_by("timestamp")
    assert list(events.values_list("action", flat=True)) == [
        "authorization.department_managed_mail_eligibility_granted",
        "authorization.department_managed_mail_eligibility_revoked",
    ]
    assert list(events.values_list("metadata", flat=True)) == [
        {"allowed": True},
        {"allowed": False},
    ]


@pytest.mark.django_db
def test_eligibility_is_independent_from_system_provider_state(eligibility_scope):
    system_admin, _, department = eligibility_scope
    grant_system_managed_mail_eligibility(actor=system_admin, department=department)
    configuration, _ = SystemMailConfiguration.objects.get_or_create(singleton=True)
    configuration.delivery_mode = SystemMailConfiguration.DeliveryMode.DISABLED
    configuration.save(update_fields=("delivery_mode",))
    assert is_system_managed_mail_allowed(department=department)

    configuration.delivery_mode = SystemMailConfiguration.DeliveryMode.SMTP
    configuration.last_verification_outcome = "FAILED"
    configuration.save(update_fields=("delivery_mode", "last_verification_outcome"))
    assert is_system_managed_mail_allowed(department=department)
