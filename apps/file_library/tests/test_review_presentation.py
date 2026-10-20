# ruff: noqa: F811
"""Current assessment owns status presentation; GETs never reconcile reviews."""

import importlib
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.file_library import services
from apps.file_library.capacity import review_management_url
from apps.file_library.models import CapacityReview, Library
from apps.file_library.publication import publication_state, scope_kwargs
from apps.file_library.publication_reconciliation import reconcile_file_scope
from apps.file_library.tests.test_capacity import approve
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import crypto, publish  # noqa: F401
from apps.file_library.tests.test_publication_capacity_ux import button, risky_station
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import enqueue_publication_job
from apps.tablets.models import AppInstallation

pytestmark = pytest.mark.django_db


def assert_read_only(queries):
    assert not [
        query["sql"]
        for query in queries
        if query["sql"].lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"}
    ]


@pytest.mark.parametrize("department_library", [False, True])
@pytest.mark.parametrize("hx", [False, True])
@pytest.mark.parametrize(
    "scenario",
    [
        "pending",
        "rejected",
        "approved",
        "old_source",
        "old_telemetry",
        "wrong_binding",
        "expired",
        "absent",
    ],
)
def test_only_current_review_controls_presentation_and_gets_are_read_only(
    client, scope, pdf_pipeline, department_library, hx, scenario
):
    department_lib = library_for(scope) if department_library else None
    library, installation, review = risky_station(scope)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    if scenario in {"approved", "rejected"}:
        approve(scope[0], review, approve=scenario == "approved")
    elif scenario == "old_source":
        old = deepcopy(review.publication_snapshot)
        old["source_revision"] -= 1
        CapacityReview.objects.filter(pk=review.pk).update(publication_snapshot=old)
    elif scenario == "old_telemetry":
        AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=2)
    elif scenario == "wrong_binding":
        other = DatasetPublication.objects.create(
            scope_state=state,
            department=scope[3],
            station=scope[5],
            scope_type="STATION",
            dataset_type_code="station_files",
            version_number=99,
            schema_version=2,
            source_revision=library.source_revision,
            status="CANCELLED",
        )
        CapacityReview.objects.filter(pk=review.pk).update(publication=other)
    elif scenario == "expired":
        CapacityReview.objects.filter(pk=review.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    elif scenario == "absent":
        CapacityReview.objects.filter(pk=review.pk).update(superseded_at=timezone.now())
    applicable = scenario in {"pending", "rejected", "approved"}
    client.force_login(scope[0])
    headers = {"HTTP_HX_REQUEST": "true"} if hx else {}
    with CaptureQueriesContext(connection) as queries:
        status = publication_state(library)
        detail = client.get(reverse("publications-scope-detail", args=(state.id,)), **headers)
        poll = client.get(reverse("publications-scope-status", args=(state.id,)), **headers)
    assert_read_only(queries)
    assert bool(status["capacity_review"]) == applicable
    assert bool(status["capacity_summary"]) == applicable
    assert status["capacity_ready"] == (scenario == "approved")
    if applicable:
        assert status["capacity_review"].id == review.id
    for response in (detail, poll):
        assert response.status_code == 200
        assert ("disabled" not in button(response, "Build now")) == (scenario == "approved")
        html = response.content.decode()
        if applicable:
            assert {"pending": "Awaiting approval", "rejected": "Rejected", "approved": "Eligible"}[
                scenario
            ] in html
            url = (
                reverse("file-library", args=(department_lib.id,))
                if department_lib
                else reverse("file-library-capacity-review", args=(review.id,))
            )
            assert url in html
        else:
            assert "Capacity: Assessment required" in html
            assert "Awaiting approval" not in html
            assert "Review publication capacity in File Management" not in html
    assert Library.objects.filter(department=scope[3]).exists() == department_library
    client.force_login(scope[2])
    with CaptureQueriesContext(connection) as queries:
        assert (
            client.get(
                reverse("publications-scope-detail", args=(state.id,)), **headers
            ).status_code
            == 403
        )
    assert_read_only(queries)


@pytest.mark.parametrize("hx", [False, True])
def test_clean_revert_hides_historical_rejection_and_inspection_is_read_only(
    client, scope, pdf_pipeline, crypto, hx
):
    library = services.get_library(scope[1], station_id=scope[5].id)
    active = publish(scope[1], library)
    from apps.file_library.tests.test_capacity import tablet_for

    tablet_for(scope, free=1, total=1000)
    entry = services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    review = CapacityReview.objects.get(library=library, superseded_at=None)
    approve(scope[0], review, approve=False)
    services.delete_entry(actor=scope[1], library_id=library.id, entry_id=entry.id)
    # A legacy lingering rejected row must not leak into current presentation.
    CapacityReview.objects.filter(pk=review.pk).update(superseded_at=None)
    client.force_login(scope[0])
    headers = {"HTTP_HX_REQUEST": "true"} if hx else {}
    with CaptureQueriesContext(connection) as queries:
        status = publication_state(library)
        for route in (
            "publications-scope-detail",
            "publications-scope-status",
            "publications-inspect-changes",
        ):
            response = client.get(reverse(route, args=(active.scope_state_id,)), **headers)
            assert response.status_code == 200
            assert b"Awaiting approval" not in response.content
        detail = client.get(
            reverse("publications-scope-detail", args=(active.scope_state_id,)), **headers
        )
    assert_read_only(queries)
    assert status["label"] == "Current" and status["capacity_review"] is None
    assert b"Capacity: Current" in detail.content
    client.force_login(scope[1])
    with CaptureQueriesContext(connection) as queries:
        management = client.get(reverse("file-library", args=(library.id,)), **headers)
    assert_read_only(queries)
    assert management.context["publication"]["label"] == "Current"
    review.refresh_from_db()
    assert review.decision == "rejected" and review.superseded_at is None


def test_management_fallback_is_read_only_and_does_not_cross_tenants(scope, pdf_pipeline):
    library, _, review = risky_station(scope)
    with CaptureQueriesContext(connection) as queries:
        assert review_management_url(scope[3], review) == reverse(
            "file-library-capacity-review", args=(review.id,)
        )
        assert review_management_url(scope[4], review) == reverse("file-library-index")
        assert review_management_url(scope[3]) == reverse("file-library-index")
    assert_read_only(queries)
    assert not Library.objects.filter(department__isnull=False).exists()


def test_latest_review_does_not_override_matching_assessment(scope, pdf_pipeline):
    library, _, matching = risky_station(scope)
    newer = CapacityReview.objects.create(
        library=library,
        operation=matching.operation,
        projection=matching.projection,
        fingerprint="outdated projection",
        publication_snapshot=matching.publication_snapshot,
    )
    with CaptureQueriesContext(connection) as queries:
        status = publication_state(library)
    assert_read_only(queries)
    assert status["capacity_review"].pk == matching.pk
    newer.refresh_from_db()
    assert newer.superseded_at is None


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


def test_corrected_legacy_bulk_job_can_reconcile_clean_source(scope, pdf_pipeline, crypto):
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
    cutoff = (
        MigrationRecorder(connection)
        .migration_qs.get(app="publications", name="0029_publicationjob_allow_clean_rebuild")
        .applied
    )
    PublicationJob.objects.filter(pk=job.pk).update(created_at=cutoff - timedelta(seconds=1))
    migration = importlib.import_module(
        "apps.publications.migrations.0030_correct_legacy_bulk_rebuild_intent"
    )
    migration.correct_legacy_bulk_requests(apps, SimpleNamespace(connection=connection))
    job.refresh_from_db()
    assert not job.allow_clean_rebuild
    reconcile_file_scope(scope_id=job.scope_state_id, department_id=scope[3].id, apply=True)
    job.refresh_from_db()
    job.build_publication.refresh_from_db()
    assert job.status == "CANCELLED" and job.build_publication.status == "CANCELLED"
    active.scope_state.refresh_from_db()
    assert active.scope_state.current_published_publication_id == active.pk
