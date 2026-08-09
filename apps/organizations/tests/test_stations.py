import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.organizations.services import create_station


@pytest.fixture
def department_admin():
    actor = User.objects.create_user("admin@example.test", "Department Admin", "safe-password")
    department = Department.objects.create(name="Department", short_code="DEP", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    return actor, department


@pytest.mark.django_db
def test_create_station_uses_validated_active_value_and_records_audit_event(department_admin):
    actor, department = department_admin

    active_station = create_station(
        actor=actor, department=department, name="Active", short_code="ACT", active=True
    )
    inactive_station = create_station(
        actor=actor, department=department, name="Inactive", short_code="INA", active=False
    )

    assert active_station.active is True
    assert inactive_station.active is False
    assert active_station.department_id == department.id
    assert inactive_station.department_id == department.id
    assert AuditEvent.objects.filter(
        action="organization.station_created", actor_user=actor, target_uuid=active_station.id
    ).exists()
    assert AuditEvent.objects.filter(
        action="organization.station_created", actor_user=actor, target_uuid=inactive_station.id
    ).exists()


@pytest.mark.django_db
def test_create_station_defaults_to_active_and_denies_unscoped_actor(department_admin):
    actor, department = department_admin
    default_station = create_station(
        actor=actor, department=department, name="Default", short_code="DEF"
    )
    outsider = User.objects.create_user("outsider@example.test", "Outsider", "safe-password")

    assert default_station.active is True
    with pytest.raises(PermissionDenied, match="Department administrator scope"):
        create_station(actor=outsider, department=department, name="Denied", short_code="NO")
