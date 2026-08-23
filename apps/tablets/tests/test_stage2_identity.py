"""Stage 2 identity uniqueness regression tests (PostgreSQL-backed)."""

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.tablets.models import Tablet
from apps.tablets.services import TabletError, create_tablet


@pytest.fixture
def identity_scope(db):
    admin = User.objects.create_user("identity@example.test", "Identity", "safe-password")
    other_admin = User.objects.create_user("identity2@example.test", "Identity2", "safe-password")
    department = Department.objects.create(name="Alpha", short_code="ALP", created_by=admin)
    other = Department.objects.create(name="Bravo", short_code="BRV", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(user=other_admin, department=other, created_by=admin)
    return admin, other_admin, department, other


@pytest.mark.django_db
def test_duplicate_display_name_same_department_rejected(identity_scope):
    admin, _, department, _ = identity_scope
    create_tablet(actor=admin, department=department, display_name="Command iPad")
    with pytest.raises(TabletError, match="display name"):
        create_tablet(actor=admin, department=department, display_name="Command iPad")


@pytest.mark.django_db
def test_same_display_name_different_departments_permitted(identity_scope):
    admin, other_admin, department, other = identity_scope
    create_tablet(actor=admin, department=department, display_name="Command iPad")
    tablet = create_tablet(actor=other_admin, department=other, display_name="Command iPad")
    assert tablet.department == other


@pytest.mark.django_db
def test_duplicate_asset_number_same_department_rejected(identity_scope):
    admin, _, department, _ = identity_scope
    create_tablet(actor=admin, department=department, display_name="A", asset_number="TAB-1")
    with pytest.raises(TabletError, match="asset number"):
        create_tablet(actor=admin, department=department, display_name="B", asset_number="TAB-1")


@pytest.mark.django_db
def test_same_asset_number_different_departments_permitted(identity_scope):
    admin, other_admin, department, other = identity_scope
    create_tablet(actor=admin, department=department, display_name="A", asset_number="TAB-1")
    create_tablet(actor=other_admin, department=other, display_name="A", asset_number="TAB-1")


@pytest.mark.django_db
def test_multiple_blank_asset_numbers_permitted(identity_scope):
    admin, _, department, _ = identity_scope
    create_tablet(actor=admin, department=department, display_name="One")
    create_tablet(actor=admin, department=department, display_name="Two")
    assert Tablet.objects.filter(department=department).count() == 2


@pytest.mark.django_db
def test_database_constraint_catches_concurrent_duplicate_display_name(identity_scope):
    _, _, department, _ = identity_scope
    Tablet.objects.create(department=department, display_name="Command iPad")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Tablet.objects.create(department=department, display_name="Command iPad")


@pytest.mark.django_db
def test_database_constraint_catches_concurrent_duplicate_asset_number(identity_scope):
    _, _, department, _ = identity_scope
    Tablet.objects.create(department=department, display_name="A", asset_number="TAB-1")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Tablet.objects.create(department=department, display_name="B", asset_number="TAB-1")


@pytest.mark.django_db
def test_create_view_rejects_duplicate_display_name(client, identity_scope):
    admin, _, department, _ = identity_scope
    client.force_login(admin)
    create_tablet(actor=admin, department=department, display_name="Command iPad")
    response = client.post(
        reverse("tablet-create", args=(department.id,)),
        {"display_name": "Command iPad", "asset_number": ""},
    )
    assert response.status_code == 200
    assert Tablet.objects.filter(department=department).count() == 1
