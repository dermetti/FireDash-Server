# ruff: noqa: F811
"""Logical reverts retain immutable attempts and never grant capacity implicitly."""

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import timedelta
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.file_library import services
from apps.file_library.models import CapacityReview, Content
from apps.file_library.publication import publication_state, scope_kwargs
from apps.file_library.publication_capacity import reassess_department
from apps.file_library.publication_reconciliation import reconcile_file_scope
from apps.file_library.tests.test_capacity import approve, serialized_operations, tablet_for
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import crypto, publish  # noqa: F401
from apps.file_library.tests.test_publication_capacity import current_review
from apps.publications.builders import source_fingerprint_for_payload
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.services import (
    build_claimed_job,
    claim_next_job,
    delete_staged_publication,
    enqueue_publication_job,
    recover_stale_jobs,
    stage_publication_update,
)
from apps.publications.state import scope_operational_states_for_scopes
from apps.tablets.models import AppInstallation

pytestmark = pytest.mark.django_db


def legacy_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def old_comparisons(monkeypatch):
    monkeypatch.setattr("apps.publications.builders.source_fingerprint_for_payload", legacy_hash)
    monkeypatch.setattr("apps.publications.services.source_fingerprint_for_payload", legacy_hash)


def kwargs(library):
    return {key: value for key, value in scope_kwargs(library).items() if key != "scope_type"}


def assert_clean(library, active):
    library.refresh_from_db()
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    assert state.current_published_publication_id == active.id
    assert state.dirty_since is None
    assert state.current_source_fingerprint == active.source_fingerprint
    assert not PublicationJob.objects.filter(scope_state=state, status="PENDING").exists()
    assert not CapacityReview.objects.filter(
        library=library, superseded_at=None, consumed_at=None, publication_snapshot__isnull=False
    ).exists()
    assert scope_operational_states_for_scopes([state])[0]["state"] == "CURRENT"
    with CaptureQueriesContext(connection) as queries:
        status = publication_state(library)
    assert status["label"] == "Current" and not status["unpublished"]
    assert status["capacity_review"] is None and not status["can_publish"]
    assert not any(
        query["sql"].lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"} for query in queries
    )


@pytest.mark.parametrize("station", [False, True])
@pytest.mark.parametrize("decision", ["pending", "rejected", "approved", "superseded"])
def test_upload_delete_cancels_attempt_and_retires_unbound_review(
    scope, pdf_pipeline, crypto, station, decision
):
    actor = scope[1] if station else scope[0]
    library = services.get_library(actor, station_id=scope[5].id) if station else library_for(scope)
    active = publish(actor, library)
    _, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
    )
    doc = services.upload_pdf(actor=actor, library_id=library.id, upload=pdf())
    review = current_review(library)
    assert review.publication_id is None
    staged = DatasetPublication.objects.get(scope_state=active.scope_state, status="STAGED")
    frozen = deepcopy(staged.source_snapshot)
    if decision in {"approved", "rejected"}:
        approve(scope[0], review, approve=decision == "approved")
    elif decision == "superseded":
        CapacityReview.objects.filter(pk=review.pk).update(superseded_at=timezone.now())
    services.delete_entry(actor=actor, library_id=library.id, entry_id=doc.id)
    assert_clean(library, active)
    review.refresh_from_db()
    staged.refresh_from_db()
    assert review.superseded_at and review.consumed_at is None
    assert review.decision == (decision if decision != "superseded" else "pending")
    assert staged.status == "CANCELLED" and staged.source_snapshot == frozen
    with pytest.raises(services.LibraryError, match="no longer pending"):
        approve(scope[0], review, approve=False)
    before = DatasetPublication.objects.count()
    stage_publication_update(actor=scope[0], **kwargs(library))
    assert DatasetPublication.objects.count() == before


@pytest.mark.parametrize("repair", ["stage", "command"])
def test_production_legacy_upload_delete_reject_delete_staged_then_stage(
    scope, pdf_pipeline, crypto, monkeypatch, client, repair
):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    # Reproduce the old algorithm and old terminal-review omission exactly.
    with monkeypatch.context() as old:
        old_comparisons(old)
        for name in ("Guides", "Plans", "Manuals"):
            services.create_folder(**common, name=name)
        services.upload_pdf(**common, upload=pdf("remaining.pdf"))
        active = publish(scope[0], library)
        _, installation = tablet_for(scope)
        AppInstallation.objects.filter(pk=installation.pk).update(
            storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
        )
        doc = services.upload_pdf(**common, upload=pdf())
        services.delete_entry(**common, entry_id=doc.id)
        review = current_review(library)
        assert len(review.publication_snapshot["documents"]) == 1
        approve(scope[0], review, approve=False)
        staged = DatasetPublication.objects.get(scope_state=active.scope_state, status="STAGED")
        old.setattr(
            "apps.file_library.publication_capacity.retire_attempt_review", lambda *a, **k: None
        )
        delete_staged_publication(actor=scope[0], publication=staged)
        # Already affected installations may also have retried staging v4.
        if repair == "command":
            enqueue_publication_job(**kwargs(library))
    review.refresh_from_db()
    assert review.decision == "rejected" and review.superseded_at is None
    assert active.source_fingerprint == legacy_hash(active.source_snapshot)
    assert active.source_fingerprint != source_fingerprint_for_payload(active.source_snapshot)
    # Even before repair, status must not mint an approval for unchanged source.
    with CaptureQueriesContext(connection) as queries:
        status = publication_state(library)
    assert status["publication_current"] and status["capacity_review"] is None
    assert not any(q["sql"].lstrip().startswith(("UPDATE ", "DELETE ", "INSERT ")) for q in queries)
    before = DatasetPublication.objects.count()
    if repair == "stage":
        stage_publication_update(actor=scope[0], **kwargs(library))
    else:
        call_command(
            "reconcile_file_publications",
            department=str(scope[3].id),
            apply=True,
            stdout=StringIO(),
        )
    assert DatasetPublication.objects.count() == before
    assert_clean(library, active)
    assert library.source_revision > active.source_revision
    review.refresh_from_db()
    assert review.decision == "rejected" and review.superseded_at and not review.consumed_at
    client.force_login(scope[0])
    response = client.get(reverse("publications-list", args=(scope[3].id,)), {"state": "current"})
    assert active.scope_state_id in {row["scope_id"] for row in response.context["scope_rows"]}


@pytest.mark.parametrize(
    "change", ["library_name", "folder_name", "document_name", "move", "replace"]
)
@pytest.mark.parametrize("legacy", [False, True])
def test_metadata_and_same_pdf_reverts_compare_logically(
    scope, pdf_pipeline, crypto, monkeypatch, change, legacy
):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    folder = services.create_folder(**common, name="Guides")
    upload = pdf()
    original_bytes = upload.read()
    upload.seek(0)
    doc = services.upload_pdf(**common, upload=upload)
    if legacy:
        with monkeypatch.context() as old:
            old_comparisons(old)
            active = publish(scope[0], library)
    else:
        active = publish(scope[0], library)
    original_snapshot = deepcopy(active.source_snapshot)
    if change == "library_name":
        original_name = library.name
        services.rename_library(**common, name="Changed")
        services.rename_library(**common, name=original_name)
    elif change in {"folder_name", "document_name"}:
        entry = folder if change == "folder_name" else doc
        services.edit_entry(
            **common, entry_id=entry.id, name="Changed" if entry.is_folder else "Changed.pdf"
        )
        services.edit_entry(**common, entry_id=entry.id, name=entry.name)
    elif change == "move":
        services.edit_entry(**common, entry_id=doc.id, parent_id=folder.id, move=True)
        services.edit_entry(**common, entry_id=doc.id, parent_id=None, move=True)
    else:
        services.upload_pdf(**common, replace_id=doc.id, upload=pdf(pages=2))
        replacement = services.upload_pdf(
            **common, replace_id=doc.id, upload=SimpleUploadedFile("guide.pdf", original_bytes)
        )
        assert replacement.content_id != doc.content_id
        assert replacement.content.sha256 == doc.content.sha256
    assert_clean(library, active)
    active.refresh_from_db()
    assert active.source_snapshot == original_snapshot
    assert active.source_snapshot["source_revision"] == active.source_revision
    assert Content.objects.filter(pk=doc.content_id).exists()


def test_superseded_equality_still_publishes_new_version_and_reuses_artifact(
    scope, pdf_pipeline, crypto
):
    library = library_for(scope)
    common = dict(actor=scope[0], library_id=library.id)
    upload = pdf()
    original_bytes = upload.read()
    upload.seek(0)
    doc = services.upload_pdf(**common, upload=upload)
    first = publish(scope[0], library)
    original_artifact = first.document_artifact_references.get().document_artifact_id
    services.upload_pdf(**common, replace_id=doc.id, upload=pdf(pages=2))
    second = publish(scope[0], library)
    assert second.document_artifact_references.get().document_artifact_id != original_artifact
    _, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
    )
    services.upload_pdf(
        **common, replace_id=doc.id, upload=SimpleUploadedFile("guide.pdf", original_bytes)
    )
    job = PublicationJob.objects.get(scope_state=first.scope_state, status="PENDING")
    attempt_id = job.build_publication_id
    assert job.build_publication is not None
    version = job.build_publication.version_number
    for _ in range(3):
        enqueue_publication_job(**kwargs(library))
    assert (
        PublicationJob.objects.filter(scope_state=first.scope_state, status="PENDING").count() == 1
    )
    job.refresh_from_db()
    assert job.build_publication is not None
    assert job.build_publication_id == attempt_id
    assert job.build_publication.source_fingerprint == first.source_fingerprint
    assert job.build_publication.source_fingerprint != second.source_fingerprint
    assert not publication_state(library)["can_publish"]
    review = current_review(library)
    approve(scope[0], review)
    third = publish(scope[0], library)
    assert third.id == attempt_id and third.version_number == version
    assert third.status == "PUBLISHED" and third.supersedes_id == second.id
    assert third.document_artifact_references.get().document_artifact_id == original_artifact


@pytest.mark.parametrize("legacy", [False, True])
def test_real_content_change_still_requires_unknown_capacity_approval(
    scope, pdf_pipeline, crypto, monkeypatch, legacy
):
    library = library_for(scope)
    if legacy:
        with monkeypatch.context() as old:
            old_comparisons(old)
            active = publish(scope[0], library)
    else:
        active = publish(scope[0], library)
    _, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
    )
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    review = current_review(library)
    assert review.projection["tablets"][0]["state"] == "unknown"
    PublicationJob.objects.filter(status="PENDING").update(not_before=timezone.now())
    assert claim_next_job() is None
    approve(scope[0], review)
    job = claim_next_job()
    assert job is not None
    result = build_claimed_job(job_id=job.id)
    assert result.status == "SUCCEEDED"
    review.refresh_from_db()
    assert review.consumed_at and review.publication_id != active.id


def test_reconcile_dry_run_repeatability_and_scope_isolation(
    scope, pdf_pipeline, crypto, monkeypatch
):
    library = library_for(scope)
    with monkeypatch.context() as old:
        old_comparisons(old)
        active = publish(scope[0], library)
        services.rename_library(actor=scope[0], library_id=library.id, name="Other")
        services.rename_library(actor=scope[0], library_id=library.id, name=library.name)
    station_library = services.get_library(scope[1], station_id=scope[5].id)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[1], library_id=station_library.id, upload=pdf())
    other_library = services.get_library(scope[2], department_id=scope[4].id)
    services.upload_pdf(actor=scope[2], library_id=other_library.id, upload=pdf())
    station_review = current_review(station_library)
    count = DatasetPublication.objects.count()
    output = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command("reconcile_file_publications", department=str(scope[3].id), stdout=output)
    assert "DRY RUN" in output.getvalue() and "eligible" in output.getvalue()
    assert str(other_library.id) not in output.getvalue()
    assert not any(q["sql"].lstrip().startswith(("UPDATE ", "DELETE ", "INSERT ")) for q in queries)
    call_command(
        "reconcile_file_publications", department=str(scope[3].id), apply=True, stdout=StringIO()
    )
    assert_clean(library, active)
    with CaptureQueriesContext(connection) as queries:
        call_command(
            "reconcile_file_publications",
            department=str(scope[3].id),
            apply=True,
            stdout=StringIO(),
        )
    assert not any(q["sql"].lstrip().startswith(("UPDATE ", "DELETE ", "INSERT ")) for q in queries)
    assert DatasetPublication.objects.count() == count
    station_review.refresh_from_db()
    assert station_review.superseded_at is None
    assert PublicationJob.objects.filter(
        scope_state__department=scope[4], status="PENDING"
    ).exists()
    with pytest.raises(DatasetScopeState.DoesNotExist):
        reconcile_file_scope(scope_id=active.scope_state_id, department_id=scope[4].id, apply=True)
    with pytest.raises(PermissionDenied):
        stage_publication_update(actor=scope[2], **kwargs(library))


@pytest.mark.django_db(transaction=True)
def test_running_build_and_revert_do_not_queue_redundant_successor(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    active = publish(scope[0], library)
    common = dict(actor=scope[0], library_id=library.id)
    services.rename_library(**common, name="Building")
    PublicationJob.objects.filter(status="PENDING").update(not_before=timezone.now())
    job = claim_next_job()
    assert job is not None
    assert job.build_publication is not None
    frozen = deepcopy(job.build_publication.source_snapshot)
    serialized_operations(
        lambda: services.rename_library(**common, name=library.name),
        lambda: reconcile_file_scope(
            scope_id=active.scope_state_id, department_id=scope[3].id, apply=True
        ),
    )
    job.build_publication.refresh_from_db()
    assert job.build_publication.source_snapshot == frozen
    assert job.build_publication.status == "BUILDING"
    result = build_claimed_job(job_id=job.id)
    assert result.status == "OBSOLETE"
    assert_clean(library, active)


def test_explicit_clean_rebuild_is_not_cancelled_or_exempted_from_capacity(scope, crypto):
    library = library_for(scope)
    active = publish(scope[0], library)
    _, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
    )
    job = enqueue_publication_job(
        **kwargs(library), allow_clean_rebuild=True, trigger_type="USER_REQUEST"
    )
    assert job is not None and job.allow_clean_rebuild
    reconcile_file_scope(scope_id=active.scope_state_id, department_id=scope[3].id, apply=True)
    job.refresh_from_db()
    assert job.status == "PENDING" and current_review(library).superseded_at is None
    assert claim_next_job() is None
    approve(scope[0], current_review(library))
    claimed = claim_next_job()
    assert claimed is not None and claimed.build_publication_id is not None
    failed_attempt = claimed.build_publication_id
    PublicationJob.objects.filter(pk=job.id).update(
        heartbeat_at=timezone.now() - timedelta(hours=1)
    )
    assert recover_stale_jobs(timeout=timedelta(minutes=5)) == 1
    reassess_department(scope[3].id)
    fresh = current_review(library)
    reconcile_file_scope(scope_id=active.scope_state_id, department_id=scope[3].id, apply=True)
    fresh.refresh_from_db()
    assert fresh.superseded_at is None
    approve(scope[0], fresh)
    retry = claim_next_job()
    assert retry is not None and retry.build_publication_id != failed_attempt
    assert build_claimed_job(job_id=retry.id).status == "OBSOLETE"
    assert_clean(library, active)


def test_identical_changed_payload_coalesces_one_attempt_with_latest_storage_id(
    scope, pdf_pipeline, crypto
):
    library = library_for(scope)
    publish(scope[0], library)
    upload = pdf()
    raw = upload.read()
    upload.seek(0)
    common = dict(actor=scope[0], library_id=library.id)
    doc = services.upload_pdf(**common, upload=upload)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    first = PublicationJob.objects.get(scope_state=state, status="PENDING")
    assert first.build_publication is not None
    original_digest = first.build_publication.source_fingerprint
    for _ in range(2):
        services.upload_pdf(
            **common, replace_id=doc.id, upload=SimpleUploadedFile("guide.pdf", raw)
        )
    latest = PublicationJob.objects.get(scope_state=state, status="PENDING")
    assert latest.id == first.id and latest.build_publication_id == first.build_publication_id
    assert latest.build_publication is not None
    assert latest.build_publication.source_fingerprint == original_digest
    assert latest.build_publication.source_snapshot is not None
    doc.refresh_from_db()
    assert latest.build_publication.source_snapshot["documents"][0]["content_id"] == str(
        doc.content_id
    )


def test_clean_revert_preserves_consumed_approval_and_future_review(scope, pdf_pipeline, crypto):
    library = library_for(scope)
    _, installation = tablet_for(scope)
    AppInstallation.objects.filter(pk=installation.pk).update(
        storage_observed_at=None, free_storage_bytes=None, total_storage_bytes=None
    )
    common = dict(actor=scope[0], library_id=library.id)
    services.upload_pdf(**common, upload=pdf("baseline.pdf"))
    consumed = current_review(library)
    approve(scope[0], consumed)
    active = publish(scope[0], library)
    consumed.refresh_from_db()
    original_consumption = consumed.consumed_at
    assert original_consumption is not None
    extra = services.upload_pdf(**common, upload=pdf("extra.pdf"))
    pending = current_review(library)
    future_snapshot = deepcopy(pending.publication_snapshot)
    future_snapshot["source_revision"] += 100
    future = CapacityReview.objects.create(
        library=library,
        requested_by=scope[0],
        operation=pending.operation,
        projection=pending.projection,
        fingerprint=pending.fingerprint,
        publication_snapshot=future_snapshot,
    )
    services.delete_entry(**common, entry_id=extra.id)
    for review in (consumed, pending, future):
        review.refresh_from_db()
    assert consumed.consumed_at == original_consumption and consumed.superseded_at is None
    assert consumed.publication_id == active.id and consumed.decision == "approved"
    assert pending.superseded_at is not None
    assert future.superseded_at is None and future.decision == "pending"
    assert publication_state(library)["publication_current"]


@pytest.mark.parametrize("debounced", [False, True])
def test_force_flag_migration_preserves_legacy_explicit_intent(scope, debounced):
    from django.apps import apps

    library = library_for(scope)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    job = PublicationJob.objects.get(scope_state=state)
    job.trigger_type = "USER_REQUEST"
    job.debounce_started_at = timezone.now() if debounced else None
    job.save(update_fields=("trigger_type", "debounce_started_at"))
    migration = importlib.import_module(
        "apps.publications.migrations.0029_publicationjob_allow_clean_rebuild"
    )
    migration.preserve_explicit_requests(apps, None)
    job.refresh_from_db()
    assert job.allow_clean_rebuild is not debounced
