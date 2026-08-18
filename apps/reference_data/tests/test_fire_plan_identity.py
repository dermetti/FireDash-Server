import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.reference_data.models import FirePlan


@pytest.fixture
def fire_plan_context(db):
    actor = User.objects.create_user("fire-plan-identity@example.test", "Identity", "safe-password")
    department = Department.objects.create(name="Identity", short_code="FID", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    return actor, department


def _plan(*, actor, department, external_identifier="", address="", object_name=""):
    return FirePlan.objects.create(
        department=department,
        external_identifier=external_identifier,
        address=address,
        object_name=object_name,
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="plan.pdf",
        file_size=1,
        page_count=1,
        sha256="a" * 64,
        uploaded_by=actor,
    )


@pytest.mark.django_db
def test_fire_plan_database_identity_constraints(fire_plan_context):
    actor, department = fire_plan_context
    _plan(actor=actor, department=department, external_identifier="PLAN-123")
    _plan(actor=actor, department=department, address="Wandsbeker Zollstraße 95")
    for kwargs in (
        {},
        {"external_identifier": "PLAN-123"},
        {"address": "Wandsbeker Zollstraße 95"},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            _plan(actor=actor, department=department, **kwargs)
