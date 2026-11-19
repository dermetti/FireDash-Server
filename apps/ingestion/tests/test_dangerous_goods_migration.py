"""The permanent dangerous-goods registry enforces system scope."""

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState, DatasetTypeRegistry


@pytest.mark.django_db
def test_dangerous_goods_registry_refuses_department_scope():
    registry = DatasetTypeRegistry.objects.get(code="dangerous_goods")
    assert registry.scope == "system" and registry.current_schema_version == 1
    user = User.objects.create_user("goods@example.test", "Goods", "password")
    department = Department.objects.create(name="Goods", short_code="GOODS", created_by=user)
    with pytest.raises(ValidationError):
        DatasetScopeState(
            scope_type="DEPARTMENT", department=department, dataset_type_code="dangerous_goods"
        ).full_clean()
    with pytest.raises(DatabaseError), transaction.atomic():
        DatasetScopeState.objects.create(
            scope_type="SYSTEM", department=department, dataset_type_code="dangerous_goods"
        )
    scope, _ = DatasetScopeState.objects.get_or_create(
        scope_type="SYSTEM", dataset_type_code="dangerous_goods"
    )
    assert scope.department_id is None and scope.station_id is None
