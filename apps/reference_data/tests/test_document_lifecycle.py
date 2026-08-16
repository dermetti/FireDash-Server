import uuid

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department
from apps.publications.feature_services import set_department_feature
from apps.publications.models import DatasetScopeState
from apps.reference_data.models import FirePlan, KlgvPlan
from apps.reference_data.services import set_fire_plan_active, set_klgv_plan_active


@pytest.fixture
def documents(db):
    actor = User.objects.create_user("documents@example.test", "Documents", "safe-password")
    department = Department.objects.create(name="Documents", short_code="DOC", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    return actor, department, other


def _fire_plan(actor, department):
    return FirePlan.objects.create(
        department=department,
        external_identifier="FP-1",
        object_name="Plan",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="plan.pdf",
        file_size=1,
        page_count=1,
        sha256="a" * 64,
        uploaded_by=actor,
    )


def _klgv_plan(actor, department):
    return KlgvPlan.objects.create(
        department=department,
        external_identifier="K-1",
        title="KLGV",
        document_key=f"{uuid.uuid4()}.pdf",
        original_filename="plan.pdf",
        file_size=1,
        page_count=1,
        source_pdf_sha256="a" * 64,
        sanitized_pdf_sha256="b" * 64,
        uploaded_by=actor,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("factory", "setter", "dataset", "event"),
    [
        (
            _fire_plan,
            set_fire_plan_active,
            "department_fire_plans",
            "reference_data.fire_plan_deactivated",
        ),
        (
            _klgv_plan,
            set_klgv_plan_active,
            "department_klgv_plans",
            "reference_data.klgv_plan_deactivated",
        ),
    ],
)
def test_explicit_document_deactivation_is_idempotent_and_audited(
    documents, factory, setter, dataset, event
):
    actor, department, _ = documents
    if dataset == "department_klgv_plans":
        set_department_feature(
            actor=actor, department=department, feature_code="klgv_plans", enabled=True
        )
    plan = factory(actor, department)
    kwargs = {
        "actor": actor,
        "active": False,
        "fire_plan" if isinstance(plan, FirePlan) else "klgv_plan": plan,
    }
    setter(**kwargs)
    scope = DatasetScopeState.objects.get(department=department, dataset_type_code=dataset)
    assert (plan.active, scope.source_revision) == (False, 1)
    assert AuditEvent.objects.filter(action=event, target_uuid=plan.id).exists()
    setter(**kwargs)
    scope.refresh_from_db()
    assert scope.source_revision == 1


@pytest.mark.django_db
def test_document_lifecycle_rejects_other_department_actor(documents):
    actor, department, other = documents
    plan = _fire_plan(actor, department)
    outsider = User.objects.create_user("other@example.test", "Other", "safe-password")
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=actor)
    with pytest.raises(PermissionDenied):
        set_fire_plan_active(actor=outsider, fire_plan=plan, active=False)
