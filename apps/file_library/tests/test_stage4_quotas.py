# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.file_library import services
from apps.file_library.models import Entry
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.publications.models import PublicationJob

pytestmark = pytest.mark.django_db


def test_department_admin_sets_both_scope_quotas_without_source_publication(scope):
    admin, station_admin, other, _department, _outside, station, _second = scope
    department_library = library_for(scope)
    station_library = services.get_library(station_admin, station_id=station.id)
    revisions = (department_library.source_revision, station_library.source_revision)
    jobs = PublicationJob.objects.count()

    services.set_library_quota(actor=admin, library_id=department_library.id, quota_bytes=123456)
    services.set_library_quota(actor=admin, library_id=station_library.id, quota_bytes=654321)

    department_library.refresh_from_db()
    station_library.refresh_from_db()
    assert department_library.quota_bytes == 123456
    assert station_library.quota_bytes == 654321
    assert (department_library.source_revision, station_library.source_revision) == revisions
    assert PublicationJob.objects.count() == jobs
    events = AuditEvent.objects.filter(action="file_library.quota_changed").order_by("timestamp")
    assert [event.metadata["old_quota_bytes"] for event in events] == [
        5_000_000_000,
        2_000_000_000,
    ]
    assert [event.metadata["new_quota_bytes"] for event in events] == [123456, 654321]
    with pytest.raises(PermissionDenied):
        services.set_library_quota(
            actor=station_admin, library_id=station_library.id, quota_bytes=1
        )
    with pytest.raises(PermissionDenied):
        services.set_library_quota(actor=other, library_id=station_library.id, quota_bytes=1)


def test_below_usage_quota_blocks_growth_but_allows_metadata_reducing_and_delete(
    scope, pdf_pipeline
):
    admin = scope[0]
    library = library_for(scope)
    document = services.upload_pdf(actor=admin, library_id=library.id, upload=pdf(pages=2))
    library.refresh_from_db()
    usage = library.usage_bytes
    services.set_library_quota(actor=admin, library_id=library.id, quota_bytes=usage - 1)
    library.refresh_from_db()
    assert library.usage_bytes > library.quota_bytes

    services.edit_entry(
        actor=admin, library_id=library.id, entry_id=document.id, name="Renamed.pdf"
    )
    with pytest.raises(services.LibraryError, match="quota"):
        services.upload_pdf(actor=admin, library_id=library.id, upload=pdf("extra.pdf"))
    services.upload_pdf(
        actor=admin, library_id=library.id, upload=pdf(pages=2), replace_id=document.id
    )
    library.refresh_from_db()
    assert library.usage_bytes == usage
    reduced = services.upload_pdf(
        actor=admin, library_id=library.id, upload=pdf("smaller.pdf"), replace_id=document.id
    )
    library.refresh_from_db()
    assert reduced.id == document.id
    assert library.usage_bytes <= usage
    services.delete_entry(actor=admin, library_id=library.id, entry_id=document.id)
    library.refresh_from_db()
    assert library.usage_bytes == 0


def test_quota_settings_require_department_scope_reauth_and_show_shortcut(client, scope):
    admin, station_admin, _other, department, _outside, station, _second = scope
    station_library = services.get_library(station_admin, station_id=station.id)
    settings_url = reverse("portal-department-settings", args=(department.id,))
    client.force_login(admin)

    page = client.get(settings_url)
    assert page.status_code == 200
    blocked = client.post(
        settings_url,
        {
            "action": "file-library-quota",
            "department_amount": "1",
            "department_unit": "GB",
            "station_amount": "0.00001",
            "station_unit": "MB",
        },
    )
    assert blocked.status_code == 302
    assert blocked.url.startswith(reverse("accounts-reauthenticate"))
    station_library.refresh_from_db()
    assert station_library.quota_bytes != 10

    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    changed = client.post(
        settings_url,
        {
            "action": "file-library-quota",
            "department_amount": "1",
            "department_unit": "GB",
            "station_amount": "0.00001",
            "station_unit": "MB",
        },
    )
    assert changed.status_code == 302
    station_library.refresh_from_db()
    assert station_library.quota_bytes == 10

    client.force_login(station_admin)
    assert client.get(settings_url).status_code == 403


def test_quota_noop_preserves_temporary_access_and_rejects_invalid_values(scope):
    admin, _station_admin, _other, _department, _outside, station, _second = scope
    library = library_for(scope)
    access = services.activate_temporary_access(actor=admin, station=station)
    before = AuditEvent.objects.count()
    services.set_library_quota(actor=admin, library_id=library.id, quota_bytes=library.quota_bytes)
    for invalid in (-1, True, 1.5, 2**63):
        with pytest.raises(services.LibraryError):
            services.set_library_quota(actor=admin, library_id=library.id, quota_bytes=invalid)
    access.refresh_from_db()
    assert access.revoked_at is None
    assert AuditEvent.objects.count() == before
    services.set_library_quota(actor=admin, library_id=library.id, quota_bytes=0)
    access.refresh_from_db()
    assert access.revocation_reason == "department_mutation"


@pytest.mark.django_db(transaction=True)
def test_concurrent_upload_and_quota_update_serialize(scope, pdf_pipeline):
    admin = scope[0]
    library = library_for(scope)
    barrier = Barrier(2)

    def update_quota():
        close_old_connections()
        barrier.wait()
        services.set_library_quota(actor=admin, library_id=library.id, quota_bytes=0)
        close_old_connections()
        return "quota"

    def upload():
        close_old_connections()
        barrier.wait()
        try:
            services.upload_pdf(actor=admin, library_id=library.id, upload=pdf())
        except services.LibraryError:
            result = "rejected"
        else:
            result = "accepted"
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(update_quota), executor.submit(upload))
        results = {future.result() for future in futures}

    library.refresh_from_db()
    assert "quota" in results
    assert library.quota_bytes == 0
    documents = list(
        Entry.objects.filter(library=library, is_folder=False).select_related("content")
    )
    assert len(documents) in {0, 1}
