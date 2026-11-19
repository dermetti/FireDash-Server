# ruff: noqa: F811
"""Current assessment owns status presentation; GETs never reconcile reviews."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.file_library.models import Library
from apps.file_library.publication_reconciliation import reconcile_file_scope
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import crypto, publish  # noqa: F401
from apps.publications.models import PublicationJob
from apps.publications.services import enqueue_publication_job

pytestmark = pytest.mark.django_db


def assert_read_only(queries):
    assert not [
        query["sql"]
        for query in queries
        if query["sql"].lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"}
    ]


@pytest.mark.parametrize("hx", [False, True])
def test_details_and_inspection_without_source_library_do_not_initialize_it(
    client, scope, pdf_pipeline, crypto, hx
):
    library = library_for(scope)
    active = publish(scope[0], library)
    Library.objects.filter(pk=library.pk).delete()
    client.force_login(scope[0])
    headers = {"HTTP_HX_REQUEST": "true"} if hx else {}
    with CaptureQueriesContext(connection) as queries:
        for route in ("publications-scope-detail", "publications-inspect-changes"):
            response = client.get(reverse(route, args=(active.scope_state_id,)), **headers)
            assert response.status_code == 200
    assert_read_only(queries)
    assert not Library.objects.filter(department=scope[3]).exists()


def test_clean_bulk_job_can_reconcile_without_rebuild(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    active = publish(scope[0], library)
    job = enqueue_publication_job(
        department=scope[3],
        dataset_type_code="department_files",
        requested_by=scope[0],
        trigger_type="BULK_REQUEST",
        allow_clean_rebuild=True,
    )
    assert job is not None and job.build_publication is not None
    # Represent an already corrected job; Phase 2 never replays the conversion.
    PublicationJob.objects.filter(pk=job.pk).update(allow_clean_rebuild=False)
    job.refresh_from_db()
    assert not job.allow_clean_rebuild
    reconcile_file_scope(scope_id=job.scope_state_id, department_id=scope[3].id, apply=True)
    job.refresh_from_db()
    job.build_publication.refresh_from_db()
    assert job.status == "CANCELLED" and job.build_publication.status == "CANCELLED"
    active.scope_state.refresh_from_db()
    assert active.scope_state.current_published_publication_id == active.pk
