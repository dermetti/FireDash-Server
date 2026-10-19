# ruff: noqa: F811
"""Capacity controls and central publication administration share one assessment."""

import re
from datetime import timedelta
from html import unescape

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.authorization.models import SystemRole
from apps.file_library import services
from apps.file_library.models import CapacityReview
from apps.file_library.publication import publication_state, scope_kwargs
from apps.file_library.tests.test_capacity import approve, tablet_for
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication_capacity import current_review
from apps.organizations.models import Station
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.services import build_staged_publication, delete_staged_publication
from apps.tablets.models import AppInstallation

pytestmark = pytest.mark.django_db


def reauth(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


def button(response, label):
    match = re.search(
        r"<button\b([^>]*)>" + re.escape(label) + r"</button>", response.content.decode()
    )
    assert match is not None, label
    return match.group(1)


def risky_station(scope):
    library = services.get_library(scope[1], station_id=scope[5].id)
    _, installation = tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[1], library_id=library.id, upload=pdf())
    return library, installation, current_review(library)


def test_reviews_live_only_in_department_file_management(client, scope, pdf_pipeline):
    department_library = library_for(scope)
    station_library, _, station_review = risky_station(scope)
    services.upload_pdf(actor=scope[0], library_id=department_library.id, upload=pdf())
    department_review = current_review(department_library)
    client.force_login(scope[0])
    settings = client.get(reverse("portal-department-settings", args=(scope[3].id,)))
    assert settings.status_code == 200
    assert b"Tablet capacity reviews" not in settings.content
    assert b"File storage quotas" in settings.content
    assert (
        reverse("file-library-capacity-review", args=(station_review.id,)).encode()
        not in settings.content
    )
    response = client.get(reverse("file-library", args=(department_library.id,)))
    html = response.content.decode()
    assert html.count('id="capacity-reviews-heading"') == 1
    assert 'id="station-reviews-heading"' not in html
    for review in (department_review, station_review):
        match = re.search(rf'<tr id="capacity-review-{review.id}">(.*?)</tr>', html, re.S)
        assert match is not None
        row = match.group(1)
        assert "Known insufficient capacity" in row
        assert "1 affected" in row and "Awaiting approval" in row
        assert reverse("file-library-capacity-review", args=(review.id,)) in row
    assert scope[5].name in html
    detail = client.get(reverse("file-library-capacity-review", args=(station_review.id,)))
    assert b"Back to File Management" in detail.content
    assert b"Department System Settings</a>" not in detail.content
    assert reverse("file-library", args=(department_library.id,)).encode() in detail.content
    client.force_login(scope[1])
    assert (
        client.get(
            reverse("file-library-capacity-review", args=(department_review.id,))
        ).status_code
        == 403
    )
    assert (
        client.get(reverse("file-library", args=(station_library.id,))).context[
            "pending_review_page"
        ]
        is None
    )
    client.force_login(scope[2])
    assert (
        client.get(reverse("file-library-capacity-review", args=(station_review.id,))).status_code
        == 403
    )


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("decision", ["pending", "rejected", "superseded", "approved"])
def test_station_publish_and_department_build_share_current_capacity(
    client, scope, pdf_pipeline, unknown, decision
):
    library_for(scope)
    library, installation, review = risky_station(scope)
    if unknown:
        AppInstallation.objects.filter(pk=installation.pk).update(
            free_storage_bytes=None, total_storage_bytes=None, storage_observed_at=None
        )
        publication_state(library)
        review = current_review(library)
    if decision in {"approved", "rejected"}:
        approve(scope[0], review, approve=decision == "approved")
    elif decision == "superseded":
        services.create_folder(actor=scope[1], library_id=library.id, name="New revision")
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    client.force_login(scope[1])
    station_url = reverse("file-library", args=(library.id,))
    response = client.get(station_url)
    assert response.status_code == 200
    assert ("disabled" in button(response, "Publish Now")) == (decision != "approved")
    assert b"View review status" in response.content
    assert (b"alert alert-warning" if unknown else b"alert alert-danger") in response.content
    assert b"Approve publication risk" not in response.content
    if decision != "approved":
        before = AuditEvent.objects.filter(action="file_library.publish_requested").count()
        assert client.post(station_url, {"action": "publish"}).status_code == 302
        assert AuditEvent.objects.filter(action="file_library.publish_requested").count() == before
    client.force_login(scope[0])
    reauth(client)
    detail_url = reverse("publications-scope-detail", args=(state.id,))
    detail = client.get(detail_url)
    assert detail.status_code == 200 and b"Publish Now" not in detail.content
    assert b"Version history" in detail.content and b"Delete staged" in detail.content
    assert ("disabled" in button(detail, "Build now")) == (decision != "approved")
    assert b"File Management" in detail.content
    status = client.get(
        reverse("publications-scope-status", args=(state.id,)), HTTP_HX_REQUEST="true"
    )
    assert status.status_code == 200
    assert ("disabled" in button(status, "Build now")) == (decision != "approved")
    current = current_review(library)
    current.refresh_from_db()
    assert current.publication_id is None  # viewing controls never binds/consumes approval
    post = client.post(
        reverse("publications-scope-build-now", args=(state.id,)), HTTP_HX_REQUEST="true"
    )
    assert post.status_code == (200 if decision == "approved" else 409)
    current.refresh_from_db()
    assert current.publication_id is None and current.consumed_at is None


def test_telemetry_clear_and_stale_approval_update_controls_without_reupload(
    client, scope, pdf_pipeline
):
    library, installation, review = risky_station(scope)
    approve(scope[0], review)
    client.force_login(scope[1])
    url = reverse("file-library", args=(library.id,))
    assert "disabled" not in button(client.get(url), "Publish Now")
    AppInstallation.objects.filter(pk=installation.pk).update(free_storage_bytes=2)
    assert "disabled" in button(client.get(url), "Publish Now")
    review.refresh_from_db()
    assert review.superseded_at and current_review(library).id != review.id
    AppInstallation.objects.filter(pk=installation.pk).update(
        free_storage_bytes=900000, total_storage_bytes=1000000
    )
    assert "disabled" not in button(client.get(url), "Publish Now")
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    client.force_login(scope[0])
    detail = client.get(reverse("publications-scope-detail", args=(state.id,)))
    assert "disabled" not in button(detail, "Build now")
    assert b"Capacity: Eligible" in detail.content
    assert not CapacityReview.objects.filter(
        library=library, superseded_at=None, decision="pending"
    ).exists()
    assert library.entries.filter(is_folder=False).count() == 1


def test_department_station_publications_are_scoped_paginated_and_administrable(
    client, scope, pdf_pipeline, monkeypatch
):
    library_for(scope)
    library, _, review = risky_station(scope)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    outside_station = Station.objects.create(
        department=scope[4], name="Foreign station", short_code="EXT"
    )
    foreign = DatasetScopeState.objects.create(
        department=scope[4],
        station=outside_station,
        scope_type="STATION",
        dataset_type_code="station_files",
    )
    client.force_login(scope[0])
    monkeypatch.setattr("apps.publications.views.SCOPE_PAGE_SIZE", 1)
    url = reverse("publications-list", args=(scope[3].id,))
    first = client.get(url, {"scope": "all", "state": "scheduled"})
    assert first.context["page"].paginator.count == 2
    second = client.get(
        url, {"scope": "all", "state": "scheduled", "page": 2}, HTTP_HX_REQUEST="true"
    )
    assert second.status_code == 200
    assert scope[5].short_code.encode() in second.content
    assert reverse("publications-scope-detail", args=(state.id,)).encode() in second.content
    assert reverse("publications-scope-detail", args=(foreign.id,)).encode() not in second.content
    assert b"Review capacity and build" in second.content
    # List and row refresh never project each file scope or load source snapshots.
    assert reverse("publications-scope-build-now", args=(state.id,)).encode() not in second.content
    assert client.get(reverse("publications-scope-detail", args=(foreign.id,))).status_code == 403
    # Publication authority does not grant station authoring authority.
    assert client.get(reverse("file-library-explorer", args=(library.id,))).status_code == 403
    with pytest.raises(PermissionDenied):
        services.rename_library(actor=scope[0], library_id=library.id, name="Not allowed")
    approve(scope[0], review)
    job = build_staged_publication(actor=scope[0], scope=state)
    assert job.station_id == scope[5].id
    assert job.build_publication is not None
    deleted = delete_staged_publication(actor=scope[0], publication=job.build_publication)
    assert deleted.status == "CANCELLED"
    client.force_login(scope[1])
    assert client.get(url).status_code == 403
    client.force_login(scope[2])
    assert client.get(reverse("publications-scope-detail", args=(state.id,))).status_code == 403
    SystemRole.objects.create(user=scope[0], created_by=scope[0])
    client.force_login(scope[0])
    assert client.get(reverse("publications-scope-detail", args=(state.id,))).status_code == 403
    SystemRole.objects.create(user=scope[2], created_by=scope[0])
    client.force_login(scope[2])
    assert client.get(reverse("publications-scope-detail", args=(state.id,))).status_code == 403


def test_review_links_preserve_pending_queue_page(client, scope, pdf_pipeline):
    department_library = library_for(scope)
    library, _, review = risky_station(scope)
    # Older requests in another library exercise a queue page beyond the first.
    for _ in range(25):
        older = CapacityReview.objects.create(
            library=department_library,
            operation=review.operation,
            projection=review.projection,
            fingerprint=review.fingerprint,
            publication_snapshot={"source_revision": 0},
            expires_at=None,
        )
        CapacityReview.objects.filter(pk=older.pk).update(
            created_at=review.created_at - timedelta(seconds=1)
        )
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    client.force_login(scope[0])
    detail = client.get(reverse("publications-scope-detail", args=(state.id,)))
    match = re.search(
        r'<a href="([^"]+)">Review publication capacity in File Management</a>',
        detail.content.decode(),
    )
    assert match is not None
    link = unescape(match.group(1))
    assert link.endswith(f"?review_page=2#capacity-review-{review.id}")
    response = client.get(link)
    assert f'id="capacity-review-{review.id}"'.encode() in response.content
    review_page = client.get(reverse("file-library-capacity-review", args=(review.id,)))
    assert link.encode() in review_page.content


def test_building_and_missing_staged_candidate_disable_manual_build(client, scope, pdf_pipeline):
    library = services.get_library(scope[1], station_id=scope[5].id)
    client.force_login(scope[1])
    assert "disabled" not in button(
        client.get(reverse("file-library", args=(library.id,))), "Publish Now"
    )
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    job = PublicationJob.objects.get(scope_state=state)
    assert job.build_publication is not None
    job.status = "RUNNING"
    job.save(update_fields=("status",))
    assert "disabled" in button(
        client.get(reverse("file-library", args=(library.id,))), "Publish Now"
    )
    client.force_login(scope[0])
    detail = client.get(reverse("publications-scope-detail", args=(state.id,)))
    assert b">Build now<" not in detail.content
    job.status = "PENDING"
    job.build_publication = None
    job.save(update_fields=("status", "build_publication"))
    detail = client.get(reverse("publications-scope-detail", args=(state.id,)))
    if b">Build now<" in detail.content:
        assert "disabled" in button(detail, "Build now")


def test_department_publish_compatibility_endpoint_fails_closed_and_requires_reauth(
    client, scope, pdf_pipeline
):
    library = library_for(scope)
    tablet_for(scope, free=1, total=1000)
    services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf())
    client.force_login(scope[0])
    url = reverse("publications-department-files-publish", args=(scope[3].id,))
    response = client.post(url)
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts-reauthenticate"))
    reauth(client)
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 409 and b"Publication is unavailable" in response.content
    assert not AuditEvent.objects.filter(action="file_library.publish_requested").exists()
    review = current_review(library)
    detail = reverse("file-library-capacity-review", args=(review.id,))
    response = client.post(
        detail, {"action": "reject", "confirm": "yes", "fingerprint": review.fingerprint}
    )
    assert response.status_code == 302 and response.url == detail
    review.refresh_from_db()
    assert review.decision == "rejected" and review.decided_by_id == scope[0].id
    assert b"Back to File Management" in client.get(detail).content
    client.force_login(scope[1])
    assert client.post(detail, {"action": "approve", "confirm": "yes"}).status_code == 403


def test_central_bulk_request_includes_station_scope_without_capacity_bypass(scope, pdf_pipeline):
    from apps.publications.services import bulk_request_rebuilds, claim_next_job

    library, _, review = risky_station(scope)
    state = DatasetScopeState.objects.get(**scope_kwargs(library))
    before = PublicationJob.objects.get(scope_state=state)
    result = bulk_request_rebuilds(actor=scope[0], department=scope[3])
    assert result["promoted"] == 1
    after = PublicationJob.objects.get(scope_state=state)
    assert after.id == before.id and after.trigger_type == "BULK_REQUEST"
    assert claim_next_job() is None
    assert current_review(library).id == review.id
    assert CapacityReview.objects.filter(library=library).count() == 1
