import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.outbound_mail.recipients import ReportRecipientError, resolve_report_recipient
from apps.outbound_mail.services import set_department_recipient_policy
from apps.personnel.models import Person


@pytest.fixture
def report_scope(db):
    admin = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=admin)
    other_department = Department.objects.create(name="Other", short_code="other", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    person = Person.objects.create(
        department=department,
        display_name="Commander",
        incident_commander_eligible=True,
        incident_commander_email="commander@example.test",
        email_verified_at=timezone.now(),
    )
    return admin, department, other_department, person


def test_recipient_policy_resolves_only_current_eligible_scoped_personnel(report_scope):
    _admin, department, other_department, person = report_scope
    admitted = resolve_report_recipient(department=department, recipient_personnel_id=person.id)
    assert admitted.personnel_id == person.id
    assert admitted.email == "commander@example.test"
    foreign = Person.objects.create(
        department=other_department,
        display_name="Other commander",
        incident_commander_eligible=True,
        incident_commander_email="other@example.test",
        email_verified_at=timezone.now(),
    )
    for personnel_id in (foreign.id, person.id):
        if personnel_id == person.id:
            person.incident_commander_eligible = False
            person.save(update_fields=("incident_commander_eligible",))
        with pytest.raises(ReportRecipientError) as error:
            resolve_report_recipient(
                department=department,
                recipient_personnel_id=personnel_id,
            )
        assert error.value.code == "recipient_not_authorized"


def test_recipient_policy_uses_persisted_email_and_exact_domain_policy(report_scope):
    admin, department, _other_department, person = report_scope
    set_department_recipient_policy(
        actor=admin,
        department=department,
        restriction_enabled=True,
        approved_domains=["example.test"],
    )
    assert (
        resolve_report_recipient(department=department, recipient_personnel_id=person.id).email
        == "commander@example.test"
    )
    person.incident_commander_email = "commander@sub.example.test"
    person.save(update_fields=("incident_commander_email",))
    with pytest.raises(ReportRecipientError) as error:
        resolve_report_recipient(department=department, recipient_personnel_id=person.id)
    assert error.value.code == "recipient_domain_not_allowed"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ({"lifecycle_status": Person.LifecycleStatus.DEPARTED}, "recipient_not_authorized"),
        ({"email_verified_at": None}, "recipient_not_authorized"),
        ({"incident_commander_email": ""}, "recipient_email_unavailable"),
        ({"incident_commander_email": "private invalid email"}, "recipient_email_unavailable"),
    ],
)
def test_recipient_policy_fails_closed_on_fresh_persisted_state(report_scope, mutation, code):
    _, department, _, person = report_scope
    admitted = resolve_report_recipient(department=department, recipient_personnel_id=person.pk)
    assert person.incident_commander_email not in f"{admitted!r} {admitted!s}"
    if mutation.get("lifecycle_status") == Person.LifecycleStatus.DEPARTED:
        mutation = {**mutation, "active": False, "departed_at": timezone.now()}
    Person.objects.filter(pk=person.pk).update(**mutation)
    with pytest.raises(ReportRecipientError) as error:
        resolve_report_recipient(department=department, recipient_personnel_id=person.pk)
    assert error.value.code == code
    assert "private" not in str(error.value)


def test_recipient_policy_is_observational(report_scope):
    from apps.audit.models import AuditEvent

    _, department, _, person = report_scope
    before = AuditEvent.objects.count()
    resolve_report_recipient(department=department, recipient_personnel_id=person.pk)
    assert AuditEvent.objects.count() == before
