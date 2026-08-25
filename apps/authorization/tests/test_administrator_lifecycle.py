import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.authorization.scopes import (
    active_department_ids,
    active_station_ids,
    orphaned_departments,
)
from apps.authorization.services import (
    permanently_remove_administrator,
    provision_department_admin,
    reinstate_department_admin,
    reinstate_station_admin,
    revoke_department_admin,
    revoke_station_admin,
    suspend_department_admin,
    suspend_station_admin,
)
from apps.organizations.models import Department, Station


@pytest.fixture
def admin_scope(db):
    actor = User.objects.create_user("actor@example.test", "Actor", "password")
    second = User.objects.create_user("second@example.test", "Second", "password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=actor)
    actor_membership = DepartmentMembership.objects.create(
        user=actor, department=department, created_by=actor
    )
    second_membership = DepartmentMembership.objects.create(
        user=second, department=department, created_by=actor
    )
    station = Station.objects.create(department=department, name="One", short_code="ONE")
    station_admin = User.objects.create_user("station@example.test", "Station", "password")
    assignment = StationAdminAssignment.objects.create(
        user=station_admin, station=station, created_by=actor
    )
    return actor, second, department, actor_membership, second_membership, assignment


@pytest.mark.django_db
def test_department_lifecycle_transitions_and_effective_scope(admin_scope):
    actor, second, department, _, membership, _ = admin_scope
    assert department.id in active_department_ids(second)
    suspend_department_admin(actor=actor, membership=membership)
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.SUSPENDED
    assert department.id not in active_department_ids(second)
    reinstate_department_admin(actor=actor, membership=membership)
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.ACTIVE
    revoke_department_admin(actor=actor, membership=membership)
    membership.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.REVOKED
    with pytest.raises(ValueError):
        reinstate_department_admin(actor=actor, membership=membership)
    assert AuditEvent.objects.filter(action="authorization.department_admin_suspended").exists()
    assert AuditEvent.objects.filter(action="authorization.department_admin_reinstated").exists()
    assert AuditEvent.objects.filter(action="authorization.department_admin_revoked").exists()


@pytest.mark.django_db
def test_station_lifecycle_transitions_and_historical_scope(admin_scope):
    actor, _, _, _, _, assignment = admin_scope
    station_admin = assignment.user
    suspend_station_admin(actor=actor, assignment=assignment)
    assert not list(active_station_ids(station_admin))
    reinstate_station_admin(actor=actor, assignment=assignment)
    assert list(active_station_ids(station_admin)) == [assignment.station_id]
    revoke_station_admin(actor=actor, assignment=assignment)
    assignment.refresh_from_db()
    assert assignment.status == StationAdminAssignment.Status.REVOKED
    with pytest.raises(ValueError):
        reinstate_station_admin(actor=actor, assignment=assignment)


@pytest.mark.django_db
def test_last_effective_department_admin_cannot_be_lost(db):
    actor = User.objects.create_user("sole@example.test", "Sole", "password")
    department = Department.objects.create(name="Solo", short_code="SOL", created_by=actor)
    membership = DepartmentMembership.objects.create(
        user=actor, department=department, created_by=actor
    )
    for operation in (suspend_department_admin, revoke_department_admin):
        with pytest.raises(ValueError):
            operation(actor=actor, membership=membership)


@pytest.mark.django_db
def test_system_admin_bootstraps_only_zero_effective_departments(db):
    system = User.objects.create_user("system@example.test", "System", "password")
    SystemRole.objects.create(user=system)
    department = Department.objects.create(name="Bootstrap", short_code="BSP", created_by=system)
    token = provision_department_admin(
        actor=system,
        department=department,
        email="first@example.test",
        display_name="First",
    )
    assert token
    first = User.objects.get(email="first@example.test")
    first.is_active = True
    first.save(update_fields=("is_active",))
    with pytest.raises(PermissionDenied):
        provision_department_admin(
            actor=system,
            department=department,
            email="second@example.test",
            display_name="Second",
        )


@pytest.mark.django_db
def test_orphan_detection_and_permanent_removal_releases_email(admin_scope):
    actor, target, department, _, membership, _ = admin_scope
    orphan = Department.objects.create(name="Orphan", short_code="ORP", created_by=actor)
    assert orphan in orphaned_departments()
    permanently_remove_administrator(actor=actor, user=target, department=department)
    membership.refresh_from_db()
    target.refresh_from_db()
    assert membership.status == DepartmentMembership.Status.REVOKED
    assert not target.is_active
    assert target.email.endswith("@anonymized.invalid")
    replacement = User.objects.create_user("second@example.test", "Replacement", "password")
    assert replacement.pk != target.pk
    assert AuditEvent.objects.filter(
        action="authorization.administrator_permanently_removed"
    ).exists()
