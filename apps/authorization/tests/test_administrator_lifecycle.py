from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.authorization.scopes import (
    active_department_ids,
    active_station_ids,
    orphaned_departments,
)
from apps.authorization.services import (
    grant_station_admin,
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
    with pytest.raises(ValueError):
        permanently_remove_administrator(actor=actor, user=actor, department=department)
    actor.refresh_from_db()
    membership.refresh_from_db()
    assert actor.is_active
    assert actor.email == "sole@example.test"
    assert membership.status == "ACTIVE"


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


@pytest.mark.parametrize(
    "conflict",
    [
        "foreign",
        "foreign_suspended",
        "foreign_department",
        "foreign_department_suspended",
        "department",
        "department_suspended",
        "system",
        "pending_system",
        "station",
        "station_suspended",
        "unrelated",
    ],
)
def test_direct_station_grant_rejects_ineligible_targets_without_side_effects(
    admin_scope, conflict, client
):
    actor, _, department, _, _, assignment = admin_scope
    target = assignment.user
    station = assignment.station
    revoke_station_admin(actor=actor, assignment=assignment)
    foreign = Department.objects.create(name="Foreign", short_code="F", created_by=actor)
    other_station = Station.objects.create(
        department=foreign if conflict.startswith("foreign") else department,
        name="Other",
        short_code="O",
    )
    if conflict in {"system", "pending_system"}:
        SystemRole.objects.create(user=target)
        if conflict == "pending_system":
            target.is_active = False
            target.save(update_fields=("is_active",))
    elif "department" in conflict:
        DepartmentMembership.objects.create(
            user=target,
            department=foreign if conflict.startswith("foreign") else department,
            created_by=actor,
            status="SUSPENDED" if conflict.endswith("suspended") else "ACTIVE",
        )
    elif conflict != "unrelated":
        StationAdminAssignment.objects.create(
            user=target,
            station=other_station,
            created_by=actor,
            status="SUSPENDED" if conflict.endswith("suspended") else "ACTIVE",
        )
    else:
        target = User.objects.create_user("unrelated@example.test", "Unrelated", "password")
    before = list(StationAdminAssignment.objects.values())
    audits = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        grant_station_admin(actor=actor, user=target, station=station)
    client.force_login(actor)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    response = client.post(
        reverse("portal-department-manage", args=(department.pk,)),
        {"action": "grant-station", "user_id": target.pk, "station_id": station.pk},
    )
    assert response.status_code == 403
    assert list(StationAdminAssignment.objects.values()) == before
    assert AuditEvent.objects.count() == audits


@pytest.mark.django_db(transaction=True)
def test_concurrent_cross_department_grants_serialize_on_target_account():
    target = User.objects.create_user("race-target@example.test", "Target", "password")
    scopes = []
    for index in range(2):
        actor = User.objects.create_user(f"race-{index}@example.test", "Actor", "password")
        department = Department.objects.create(
            name=f"Department {index}", short_code=str(index), created_by=actor
        )
        DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
        station = Station.objects.create(department=department, name="Station", short_code="S")
        StationAdminAssignment.objects.create(
            user=target,
            station=station,
            created_by=actor,
            status="REVOKED",
            revoked_at=timezone.now(),
            revoked_by=actor,
        )
        scopes.append((actor, station))
    start = Barrier(2, timeout=15)

    def grant(scope):
        actor, station = scope
        try:
            start.wait()
            grant_station_admin(actor=actor, user=target, station=station)
            return True
        except PermissionDenied:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(grant, scopes))
    assert sorted(results) == [False, True]
    assert StationAdminAssignment.objects.filter(user=target, status="ACTIVE").count() == 1
    assert StationAdminAssignment.objects.filter(user=target, status="REVOKED").count() == 2
    assert AuditEvent.objects.filter(action="authorization.station_admin_granted").count() == 1


def test_valid_station_reprovision_preserves_revoked_history_and_is_idempotent(admin_scope):
    actor, _, _, _, _, assignment = admin_scope
    revoke_station_admin(actor=actor, assignment=assignment)
    new = grant_station_admin(actor=actor, user=assignment.user, station=assignment.station)
    assert new.pk != assignment.pk
    assert (
        grant_station_admin(actor=actor, user=assignment.user, station=assignment.station).pk
        == new.pk
    )
    assignment.refresh_from_db()
    assert assignment.status == "REVOKED"
    assert assignment.revoked_by == actor


def test_grant_cannot_bypass_station_suspension(admin_scope):
    actor, _, _, _, _, assignment = admin_scope
    suspend_station_admin(actor=actor, assignment=assignment)
    audits = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        grant_station_admin(actor=actor, user=assignment.user, station=assignment.station)
    assert AuditEvent.objects.count() == audits
    assert StationAdminAssignment.objects.filter(user=assignment.user).count() == 1
    reinstate_station_admin(actor=actor, assignment=assignment)
    assignment.refresh_from_db()
    assert assignment.status == "ACTIVE"


@pytest.mark.parametrize("inactive", ["station", "department"])
def test_station_grant_checks_persisted_operational_state(admin_scope, inactive):
    actor, _, department, _, _, assignment = admin_scope
    station = assignment.station
    if inactive == "station":
        Station.objects.filter(pk=station.pk).update(active=False)
    else:
        Department.objects.filter(pk=department.pk).update(status="SUSPENDED")
    audits = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        grant_station_admin(actor=actor, user=assignment.user, station=station)
    assert AuditEvent.objects.count() == audits


@pytest.mark.parametrize("kind", ["department", "station"])
@pytest.mark.parametrize("conflict", ["system", "department", "station", "foreign_suspended"])
def test_reinstatement_rechecks_target_eligibility(admin_scope, kind, conflict):
    actor, second, department, _, membership, assignment = admin_scope
    if kind == "department":
        target = second
        suspend_department_admin(actor=actor, membership=membership)
    else:
        target = assignment.user
        suspend_station_admin(actor=actor, assignment=assignment)
    foreign = Department.objects.create(name="Foreign", short_code="F", created_by=actor)
    if conflict == "system":
        SystemRole.objects.create(user=target)
    elif conflict == "department":
        DepartmentMembership.objects.create(user=target, department=foreign, created_by=actor)
    else:
        station = Station.objects.create(department=foreign, name="Other", short_code="O")
        StationAdminAssignment.objects.create(
            user=target,
            station=station,
            created_by=actor,
            status="SUSPENDED" if conflict == "foreign_suspended" else "ACTIVE",
        )
    audits = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        if kind == "department":
            reinstate_department_admin(actor=actor, membership=membership)
        else:
            reinstate_station_admin(actor=actor, assignment=assignment)
    row = membership if kind == "department" else assignment
    row.refresh_from_db()
    assert row.status == "SUSPENDED"
    assert AuditEvent.objects.count() == audits


@pytest.mark.parametrize("authority", ["department", "station", "pending_system"])
def test_permanent_removal_protects_unrelated_suspended_and_pending_system_authority(
    admin_scope, authority
):
    actor, target, department, _, membership, _ = admin_scope
    if authority == "pending_system":
        target.is_active = False
        target.save(update_fields=("is_active",))
        SystemRole.objects.create(user=target)
    else:
        foreign = Department.objects.create(name="Foreign", short_code="F", created_by=actor)
        if authority == "department":
            DepartmentMembership.objects.create(
                user=target, department=foreign, created_by=actor, status="SUSPENDED"
            )
        else:
            station = Station.objects.create(department=foreign, name="Other", short_code="O")
            StationAdminAssignment.objects.create(
                user=target, station=station, created_by=actor, status="SUSPENDED"
            )
    before = User.objects.filter(pk=target.pk).values().get()
    audits = AuditEvent.objects.count()
    with pytest.raises(ValueError):
        permanently_remove_administrator(actor=actor, user=target, department=department)
    assert User.objects.filter(pk=target.pk).values().get() == before
    membership.refresh_from_db()
    assert membership.status == "ACTIVE"
    assert AuditEvent.objects.count() == audits
