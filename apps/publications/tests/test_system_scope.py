import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.publications.manifests import _publication_scope_payload
from apps.publications.models import DatasetPublication, DatasetScopeState


@pytest.mark.django_db
def test_system_scope_has_no_tenant_owner_and_is_explicitly_canonical():
    scope = DatasetScopeState(scope_type="SYSTEM", dataset_type_code="test_system_dataset")
    scope.full_clean()
    scope.save()
    publication = DatasetPublication(
        scope_type="SYSTEM", dataset_type_code="test_system_dataset", scope_state=scope,
        version_number=1, schema_version=1, source_revision=1,
    )
    publication.full_clean(exclude={"created_by", "published_by", "build_summary", "change_summary"})
    assert _publication_scope_payload(publication) == {"type": "SYSTEM"}


@pytest.mark.django_db
@pytest.mark.parametrize("scope_type", ["DEPARTMENT", "STATION"])
def test_invalid_owner_shapes_fail_closed(scope_type):
    # Database constraints cover bulk/raw writes; model validation covers the
    # registry and FK relationship checks used by normal service writes.
    with pytest.raises(IntegrityError):
        DatasetScopeState.objects.create(
            scope_type=scope_type,
            department_id=None,
            station_id=None,
            dataset_type_code="test_system_dataset",
        )
