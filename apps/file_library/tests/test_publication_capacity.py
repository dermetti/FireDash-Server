# ruff: noqa: F811
"""Publication admission uses real source snapshots and PostgreSQL transactions."""

import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.file_library import capacity, services
from apps.file_library.models import CapacityReview, Entry
from apps.file_library.publication import publish_now, scope_kwargs, source_payload
from apps.file_library.publication_capacity import assess
from apps.file_library.tests.test_capacity import (
    approve,
    serialized_operations,
    tablet_for,
)
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import (  # noqa: F401
    crypto,
    installation_for,
    publish,
)
from apps.publications.manifests import manifest_publications
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.services import (
    build_claimed_job,
    build_staged_publication,
    cancel_publication_build,
    claim_next_job,
    delete_staged_publication,
    enqueue_publication_job,
    fail_publication_job,
    process_next_job,
    recover_stale_jobs,
)
from apps.tablets import services as tablet_services
from apps.tablets.models import AppInstallation

pytestmark = pytest.mark.django_db


def current_review(library):
    return CapacityReview.objects.get(
        library=library,
        publication_snapshot__isnull=False,
        superseded_at=None,
        consumed_at=None,
    )


def candidate(library):
    with transaction.atomic():
        capacity.lock_capacity(library)
        snapshot = source_payload(
            department=capacity.department_for(library),
            station=library.station,
        )
        scope_state = DatasetScopeState.objects.select_for_update().get(**scope_kwargs(library))
        return assess(scope=scope_state, snapshot=snapshot)


@pytest.mark.parametrize("station", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_acceptance_and_replacement_are_independent_of_device_risk(
    client,
    scope,
    pdf_pipeline,
    station,
    unknown,
):
    actor = scope[1] if station else scope[0]
    library = services.get_library(actor, station_id=scope[5].id) if station else library_for(scope)
    _, installation = tablet_for(scope, free=10, total=1000)
    if unknown:
        AppInstallation.objects.filter(pk=installation.pk).update(
            free_storage_bytes=None,
            total_storage_bytes=None,
            storage_observed_at=None,
        )
    client.force_login(actor)
    url = reverse("file-library-explorer-action", args=(library.id, "upload"))
    response = client.post(url, {"pdfs": [pdf()]}, HTTP_HX_REQUEST="true")
    assert response.status_code == 204 and response["HX-Redirect"]
    entry = Entry.objects.get(library=library)
    first_content = entry.content_id
    replacement = services.upload_pdf(
        actor=actor,
        library_id=library.id,
        replace_id=entry.id,
        upload=pdf(pages=2),
    )
    assert replacement.id == entry.id and replacement.content_id != first_content
    library.refresh_from_db()
    assert library.source_revision == 2 and library.usage_bytes == replacement.content.size_bytes
    assert services.content_path(replacement.content).exists()
    for download in (False, True):
        response = client.get(
            reverse("file-library-document", args=(library.id, entry.id)),
            {"download": "1"} if download else {},
        )
        assert response.status_code == 200
        assert b"%PDF" in b"".join(response.streaming_content)
    review = current_review(library)
    assert review.publication_snapshot["documents"][0]["content_id"] == str(replacement.content_id)
    assert review.expires_at is None and review.decision == "pending"
    assert review.projection["tablets"][0]["state"] == ("unknown" if unknown else "warning")
    assert AuditEvent.objects.filter(action="file_library.uploaded").count() == 1
    assert AuditEvent.objects.filter(action="file_library.replaced").count() == 1
    assert not DatasetScopeState.objects.get(**scope_kwargs(library)).current_published_publication


def test_candidate_counts_accepted_content_once_and_preserves_exact_threshold(scope, pdf_pipeline):
    library = library_for(scope)
    entry = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    charge = entry.content.size_bytes + 16
    _, installation = tablet_for(scope, free=charge + 2000, total=10000)
    admitted, review = candidate(library)
    assert admitted and review is None
    AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=charge + 1999)
    admitted, review = candidate(library)
    assert not admitted
    row = review.projection["tablets"][0]
    assert row["incremental_ciphertext_bytes"] == charge
    assert row["projected_free_bytes"] == 1999 and row["reason"] == "projected_low_storage"


def test_mixed_risks_show_stronger_warning_and_all_diagnostics(client, scope, pdf_pipeline):
    library = services.get_library(scope[1], station_id=scope[5].id)
    tablet_for(scope, free=10, total=1000)
    _, unknown = tablet_for(scope)
    AppInstallation.objects.filter(pk=unknown.pk).update(
        free_storage_bytes=None,
        total_storage_bytes=None,
        storage_observed_at=None,
    )
    tablet_for(scope, station=scope[6], free=1, total=1000)
    services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    review = current_review(library)
    assert len(review.projection["tablets"]) == 2
    client.force_login(scope[1])
    for url in (
        reverse("file-library", args=(library.id,)),
        reverse("file-library-capacity-review", args=(review.id,)),
    ):
        response = client.get(url)
        html = response.content.decode()
        assert "alert alert-danger" in html and "Known insufficient tablet capacity" in html
        assert "Capacity is also unknown for 1 tablet" in html
        assert "Awaiting approval" in html and 'value="retry"' not in html
        assert "Approve publication risk" not in html
    client.force_login(scope[0])
    response = client.get(reverse("file-library-capacity-review", args=(review.id,)))
    assert "Approve publication risk" in response.content.decode()
    assert "Source revision 1" in response.content.decode()
    # Department Publications remains scoped to department files. Reviewing a
    # station capacity request does not confer station publication authority.
    station_state = DatasetScopeState.objects.get(**scope_kwargs(library))
    assert (
        client.get(reverse("publications-scope-detail", args=(station_state.id,))).status_code
        == 403
    )
    department_library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=department_library.id, upload=pdf())
    department_review = current_review(department_library)
    state = DatasetScopeState.objects.get(**scope_kwargs(department_library))
    detail_url = reverse("publications-scope-detail", args=(state.id,))
    detail = client.get(detail_url)
    assert detail.status_code == 200 and b"Review publication capacity" in detail.content
    assert (
        reverse("file-library-capacity-review", args=(department_review.id,))
        in detail.content.decode()
    )
    client.force_login(scope[1])
    assert client.get(detail_url).status_code == 403


def test_all_queue_entry_points_share_gate_and_unchanged_reviews_are_idempotent(
    scope, pdf_pipeline
):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    job = PublicationJob.objects.get(scope_state=state)
    # Automatic scheduling, explicit requests, coalescing and Build Now all
    # retain one staged attempt, one review and one request audit event.
    for _ in range(2):
        enqueue_publication_job(
            **{k: v for k, v in scope_kwargs(library).items() if k != "scope_type"}
        )
        publish_now(actor=scope[0], library_id=library.id)
        build_staged_publication(actor=scope[0], scope=state)
        assert claim_next_job() is None
    assert current_review(library).id == review.id
    assert CapacityReview.objects.filter(library=library).count() == 1
    assert PublicationJob.objects.filter(scope_state=state).count() == 1
    assert AuditEvent.objects.filter(action="file_library.capacity_override_requested").count() == 1
    job.refresh_from_db()
    assert job.status == "PENDING" and job.error_category == ""
    # A blocked oldest candidate cannot starve an independent department.
    outside = services.get_library(scope[2], department_id=scope[4].id)
    publish_now(actor=scope[2], library_id=outside.id)
    claimed = claim_next_job()
    assert claimed is not None and claimed.department_id == scope[4].id


def test_rejection_stays_blocked_until_context_changes(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    approve(scope[0], review, approve=False)
    publish_now(actor=scope[0], library_id=library.id)
    assert claim_next_job() is None
    assert current_review(library).decision == "rejected"
    services.create_folder(actor=scope[0], library_id=library.id, name="New revision")
    review.refresh_from_db()
    assert review.decision == "rejected" and review.superseded_at is not None
    assert current_review(library).decision == "pending"
    with pytest.raises(services.LibraryError):
        approve(scope[0], review)


@pytest.mark.parametrize("change", ["telemetry", "unknown", "source"])
def test_promotion_rechecks_risk_and_keeps_previous_generation(
    scope,
    pdf_pipeline,
    crypto,
    settings,
    change,
):
    installation, _private, _credential, _vehicle = installation_for(scope, settings)
    library = library_for(scope)
    first = publish(scope[0], library)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    publish_now(actor=scope[0], library_id=library.id)
    job = claim_next_job()
    assert job is not None
    if change == "telemetry":
        AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=1)
    elif change == "unknown":
        AppInstallation.objects.filter(pk=installation.pk).update(
            free_storage_bytes=None,
            total_storage_bytes=None,
            storage_observed_at=None,
        )
    else:
        services.create_folder(actor=scope[0], library_id=library.id, name="New revision")
    result = build_claimed_job(job_id=job.id)
    assert result.status == "OBSOLETE"
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    assert state.current_published_publication_id == first.id
    assert PublicationJob.objects.filter(scope_state=state, status="PENDING").count() == 1
    _, _, exposed = manifest_publications(installation=installation)
    assert {p.id for p in exposed} == {first.id}
    assert Entry.objects.filter(library=library, is_folder=False).count() == 1


@pytest.mark.django_db(transaction=True)
def test_approval_resumes_automatic_publication_without_second_upload(scope, pdf_pipeline, crypto):
    library = services.get_library(scope[1], station_id=scope[5].id)
    tablet_for(scope, free=1, total=1000)
    document = services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    review = current_review(library)
    assert process_next_job() is None  # automatic job still scheduled for its build window
    approve(scope[0], review)
    job = process_next_job()
    assert job is not None and job.status == "SUCCEEDED"
    review.refresh_from_db()
    assert review.publication_id == job.build_publication_id and review.consumed_at is not None
    assert Entry.objects.get(library=library).id == document.id
    assert AuditEvent.objects.filter(action="file_library.uploaded").count() == 1


@pytest.mark.django_db(transaction=True)
def test_source_edit_serializes_before_claim_and_invalidates_approval(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    approve(scope[0], review)
    publish_now(actor=scope[0], library_id=library.id)
    _, claimed = serialized_operations(
        lambda: services.upload_pdf(
            actor=scope[0],
            library_id=library.id,
            upload=pdf("second.pdf"),
        ),
        claim_next_job,
    )
    assert claimed is None
    review.refresh_from_db()
    assert review.superseded_at is not None and review.consumed_at is None
    assert current_review(library).operation["source_revision"] == 2
    assert Entry.objects.filter(library=library).count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_decisions_record_one_approval(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)

    def competing_decision():
        with pytest.raises(services.LibraryError, match="no longer pending"):
            approve(scope[0], review)

    serialized_operations(lambda: approve(scope[0], review), competing_decision)
    assert (
        AuditEvent.objects.filter(
            action="file_library.capacity_override_approved",
            target_uuid=review.id,
        ).count()
        == 1
    )
    assert CapacityReview.objects.filter(library=library, superseded_at=None).count() == 1


def test_worker_restart_reassesses_without_transferring_approval(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    document = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    original = current_review(library)
    approve(scope[0], original)
    publish_now(actor=scope[0], library_id=library.id)
    job = claim_next_job()
    assert job is not None
    PublicationJob.objects.filter(pk=job.id).update(
        heartbeat_at=timezone.now() - timedelta(hours=1)
    )
    assert recover_stale_jobs(timeout=timedelta(minutes=10)) == 1
    assert claim_next_job() is None
    original.refresh_from_db()
    fresh = current_review(library)
    job.refresh_from_db()
    attempt_id = job.build_publication_id
    assert claim_next_job() is None
    job.refresh_from_db()
    assert job.build_publication_id == attempt_id and current_review(library).id == fresh.id
    assert original.superseded_at is not None and original.consumed_at is None
    assert fresh.id != original.id and fresh.decision == "pending"
    approve(scope[0], fresh)
    generation = publish(scope[0], library)
    fresh.refresh_from_db()
    assert fresh.consumed_at is not None and fresh.publication_id == generation.id
    assert Entry.objects.get(library=library).id == document.id


@pytest.mark.django_db(transaction=True)
def test_committed_fresh_telemetry_releases_review_without_approval(
    scope,
    pdf_pipeline,
    crypto,
    settings,
):
    installation, _private, credential, _vehicle = installation_for(scope, settings)
    AppInstallation.objects.filter(pk=installation.pk).update(
        free_storage_bytes=None,
        total_storage_bytes=None,
        storage_observed_at=None,
    )
    library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    tablet_services.check_in(
        installation=installation,
        credential=credential,
        free_storage_bytes=1_000_000,
        total_storage_bytes=1_000_000,
    )
    review.refresh_from_db()
    assert review.superseded_at is not None and review.decision == "pending"
    job = process_next_job()
    assert job is not None and job.status == "SUCCEEDED"
    assert not CapacityReview.objects.filter(decision="approved").exists()
    assert Entry.objects.filter(library=library).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_telemetry_and_confirmation_cannot_authorize_old_fingerprint(
    scope,
    pdf_pipeline,
    settings,
):
    installation, _private, credential, _vehicle = installation_for(scope, settings)
    AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=1)
    library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    serialized_operations(
        lambda: tablet_services.check_in(
            installation=installation,
            credential=credential,
            free_storage_bytes=2,
            total_storage_bytes=installation.total_storage_bytes,
        ),
        lambda: approve(scope[0], review),
    )
    review.refresh_from_db()
    assert review.superseded_at is not None and review.decision == "pending"
    assert current_review(library).fingerprint != review.fingerprint
    assert not CapacityReview.objects.filter(decision="approved").exists()


def test_historical_upload_reviews_are_retired_without_rewriting_audit(scope, pdf_pipeline):
    from apps.file_library.tests.test_management_ux import review_for

    library = library_for(scope)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    pending = review_for(scope[0], library, publication_snapshot=None)
    approved = review_for(
        scope[0],
        library,
        publication_snapshot=None,
        decision="approved",
        decided_by=scope[0],
        decided_at=timezone.now(),
    )
    ids = list(AuditEvent.objects.values_list("id", flat=True))
    library.refresh_from_db()
    revision, usage = library.source_revision, library.usage_bytes
    migration = importlib.import_module(
        "apps.file_library.migrations.0010_publication_capacity_reviews"
    )
    migration.retire_upload_reviews(apps, None)
    for review in (pending, approved):
        review.refresh_from_db()
        assert review.superseded_at is not None and review.publication_snapshot is None
        with pytest.raises(services.LibraryError, match="Historical"):
            approve(scope[0], review)
    library.refresh_from_db()
    assert (library.source_revision, library.usage_bytes) == (revision, usage)
    assert list(AuditEvent.objects.values_list("id", flat=True)) == ids


def test_approval_does_not_expire_on_old_upload_deadline(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    CapacityReview.objects.filter(pk=review.id).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    approve(scope[0], review)
    review.refresh_from_db()
    assert review.decision == "approved" and review.expires_at is None


def test_cross_department_review_is_never_decidable(scope, pdf_pipeline):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    with pytest.raises(PermissionDenied):
        approve(scope[2], review)


@pytest.mark.parametrize("approved", [False, True])
def test_station_sees_decision_without_retry_or_another_approval_prompt(
    client,
    scope,
    pdf_pipeline,
    approved,
):
    library = services.get_library(scope[1], station_id=scope[5].id)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    review = current_review(library)
    approve(scope[0], review, approve=approved)
    client.force_login(scope[1])
    html = client.get(reverse("file-library", args=(library.id,))).content.decode()
    message = "Department approval recorded" if approved else "Publication has been rejected"
    assert message in html and "Known insufficient tablet capacity" in html
    assert "Department approval is required" not in html
    assert 'value="retry"' not in html and "Approve publication risk" not in html
    assert Entry.objects.filter(library=library, is_folder=False).count() == 1


@pytest.mark.parametrize("outcome", ["staged", "building", "failed"])
def test_terminal_attempt_retires_review_without_removing_source(scope, pdf_pipeline, outcome):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    document = services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    job = PublicationJob.objects.get(scope_state=state)
    if outcome != "staged":
        approve(scope[0], review)
        publish_now(actor=scope[0], library_id=library.id)
        claimed = claim_next_job()
        assert claimed is not None and claimed.build_publication is not None
        if outcome == "failed":
            fail_publication_job(job_id=claimed.id, error_message="Encryption unavailable.")
        else:
            cancel_publication_build(actor=scope[0], publication=claimed.build_publication)
    else:
        assert job.build_publication is not None
        delete_staged_publication(actor=scope[0], publication=job.build_publication)
    job.refresh_from_db()
    review.refresh_from_db()
    assert job.status == ("FAILED" if outcome == "failed" else "CANCELLED")
    assert review.superseded_at is not None
    assert review.consumed_at is None and Entry.objects.get(library=library).id == document.id
    with pytest.raises(services.LibraryError):
        approve(scope[0], review)
    publish_now(actor=scope[0], library_id=library.id)
    assert claim_next_job() is None
    assert current_review(library).id != review.id
