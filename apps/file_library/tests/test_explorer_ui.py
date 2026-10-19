# ruff: noqa: F811
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.file_library import services
from apps.file_library.models import CapacityReview, Entry, Library
from apps.file_library.tests.test_capacity import tablet_for
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_management_ux import reauth, review_for
from apps.publications.models import DatasetScopeState, PublicationJob

pytestmark = pytest.mark.django_db


def url(library, action=None):
    return (
        reverse("file-library-explorer-action", args=(library.id, action))
        if action
        else reverse("file-library-explorer", args=(library.id,))
    )


def normal(scope, role):
    actor = scope[0] if role == "department" else scope[1]
    library = (
        library_for(scope)
        if role == "department"
        else services.get_library(actor, station_id=scope[5].id)
    )
    return actor, library


@pytest.mark.parametrize("role", ["department", "station"])
def test_same_parent_explorer_navigation_and_full_operations(client, scope, role, pdf_pipeline):
    actor, library = normal(scope, role)
    client.force_login(actor)
    parent = client.get(reverse("file-library", args=(library.id,)))
    assert parent.status_code == 200
    assert "file_library/library.html" in [template.name for template in parent.templates]
    html = parent.content.decode()
    assert url(library) in html and "Open File Explorer" in html
    assert 'name="pdfs"' not in html and 'aria-label="Folder path"' not in html
    assert ("Station file publication" in html) == (role == "station")
    assert ("Choose station" in html) == (role == "department")
    assert ("Publish Now" in html) == (role == "station")
    assert client.get(reverse("file-library-index")).url == reverse(
        "file-library", args=(library.id,)
    )
    if role == "station":
        assert (
            reverse("file-library", args=(library.id,))
            in client.get(reverse("station-data-hub", args=(scope[5].id,))).content.decode()
        )
    response = client.get(url(library))
    assert "file_library/explorer.html" in [template.name for template in response.templates]
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="file-explorer-content"' in html and 'aria-label="Folder path"' in html
    assert "Upload PDFs" in html and "New folder" in html and "Publish Now" not in html
    assert 'aria-current="page">Files' in html and "table-responsive" not in html
    for action in ("upload", "new-folder"):
        for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
            page = client.get(url(library, action), **headers)
            assert page.status_code == 200 and 'method="post"' in page.content.decode()
            assert ('class="modal fade"' in page.content.decode()) == bool(headers)
    assert client.post(url(library, "new-folder"), {"name": "Plans"}).status_code == 302
    folder = library.entries.get(name="Plans")
    uploaded = client.post(url(library, "upload"), {"folder": str(folder.id), "pdfs": [pdf()]})
    assert uploaded.status_code == 302 and uploaded.url == url(library) + "?folder=" + str(
        folder.id
    )
    document = library.entries.get(is_folder=False)
    doc_id = document.id
    listing = client.get(url(library), {"folder": folder.id})
    assert b"guide.pdf" in listing.content and b"Plans" in listing.content
    assert 'aria-label="Actions for guide.pdf"' in listing.content.decode()
    assert '<noscript><div class="d-flex flex-wrap gap-2 mt-2"' in listing.content.decode()
    for action in ("rename", "move", "replace", "delete"):
        modal = client.get(url(library, action), {"entry": document.id}, HTTP_HX_REQUEST="true")
        assert modal.status_code == 200 and b"guide.pdf" in modal.content
        assert 'id="file-explorer-form-modal"' in modal.content.decode()
    renamed = client.post(
        url(library, "rename"), {"entry": doc_id, "name": "Map.pdf"}, HTTP_HX_REQUEST="true"
    )
    assert renamed.status_code == 204 and renamed["HX-Redirect"] == url(library)
    assert (
        client.post(url(library, "move"), {"entry": doc_id, "destination": ""}).status_code == 302
    )
    assert (
        client.post(
            url(library, "replace"), {"entry": doc_id, "pdf": pdf("replacement.pdf", pages=2)}
        ).status_code
        == 302
    )
    document.refresh_from_db()
    assert document.id == doc_id and document.name == "Map.pdf" and document.parent_id is None
    for suffix in ("", "?download=1"):
        response = client.get(reverse("file-library-document", args=(library.id, doc_id)) + suffix)
        assert response.status_code == 200
        assert b"%PDF" in b"".join(response.streaming_content)
        assert response["Cache-Control"] == "private, no-store"
    missing = client.post(url(library, "delete"), {"entry": doc_id})
    assert missing.status_code == 200 and b'aria-invalid="true"' in missing.content
    assert Entry.objects.filter(pk=doc_id).exists()
    assert (
        client.post(url(library, "delete"), {"entry": doc_id, "confirm": "yes"}).status_code == 302
    )
    assert not Entry.objects.filter(pk=doc_id).exists()
    library.refresh_from_db()
    assert library.source_revision == 6 and library.usage_bytes == 0
    events = AuditEvent.objects.filter(action__startswith="file_library.")
    assert events.count() == 6 and all(event.actor_user_id == actor.id for event in events)
    assert all(event.station_id == library.station_id for event in events)


@pytest.mark.parametrize("role", ["department", "station"])
def test_search_sort_pagination_and_breadcrumbs_are_shared(client, scope, role):
    actor, library = normal(scope, role)
    root = services.create_folder(actor=actor, library_id=library.id, name="Plans")
    child = services.create_folder(
        actor=actor, library_id=library.id, parent_id=root.id, name="Nested"
    )
    for index in range(30):
        services.create_folder(
            actor=actor, library_id=library.id, parent_id=child.id, name=f"Result {index:02}"
        )
    client.force_login(actor)
    response = client.get(url(library), {"folder": child.id})
    html = response.content.decode()
    assert 'aria-current="page">Nested' in html and f"?folder={root.id}" in html
    assert "Result 24" in html and "Result 25" not in html and "Page 1 of 2" in html
    response = client.get(
        url(library),
        {"folder": root.id, "q": "Result", "sort": "name", "direction": "desc", "page": 2},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()
    assert "<!doctype" not in html and 'id="file-explorer-content"' not in html
    assert "In Files / Plans / Nested" in html and "Page 2 of 2" in html
    assert "Result 04" in html and "Result 05" not in html
    assert "q=Result" in html and "direction=desc" in html and "sort=name" in html
    restored = client.get(
        url(library), HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true"
    )
    assert b"<!doctype" in restored.content
    for sort in ("name", "size", "modified"):
        assert client.get(url(library), {"sort": sort, "direction": "desc"}).status_code == 200
    for query in (
        {"folder": "invalid"},
        {"folder": root.id, "page": 999},
        {"page": 0},
        {"page_size": 101},
        {"sort": "invalid"},
        {"direction": "invalid"},
    ):
        assert client.get(url(library), query).status_code in (400, 404)
    assert b"No matching files or folders" in client.get(url(library), {"q": "no match"}).content


@pytest.mark.parametrize("role", ["department", "station"])
def test_action_errors_preserve_values_move_browser_and_capacity_warnings(
    client, scope, role, pdf_pipeline
):
    actor, library = normal(scope, role)
    folder = services.create_folder(actor=actor, library_id=library.id, name="Folder")
    child = services.create_folder(
        actor=actor, library_id=library.id, parent_id=folder.id, name="Child"
    )
    client.force_login(actor)
    for index in range(27):
        services.create_folder(actor=actor, library_id=library.id, name=f"Destination {index:02}")
    move = client.get(url(library, "move"), {"entry": folder.id})
    assert len(move.context["destinations"]) == 25
    assert "Destination 25" not in move.content.decode()
    modal_move = client.get(url(library, "move"), {"entry": folder.id}, HTTP_HX_REQUEST="true")
    assert f'hx-get="{url(library, "move")}?entry=' in modal_move.content.decode()
    assert (
        client.get(url(library, "move"), {"entry": folder.id, "page": 2})
        .context["destinations"]
        .paginator.count
        == 27
    )
    denied = client.post(url(library, "move"), {"entry": folder.id, "destination": child.id})
    assert denied.status_code == 200 and denied.context["form"].errors
    folder.refresh_from_db()
    assert folder.parent_id is None
    document = services.upload_pdf(actor=actor, library_id=library.id, upload=pdf())
    renamed = client.post(
        url(library, "rename"), {"entry": document.id, "name": "invalid"}, HTTP_HX_REQUEST="true"
    )
    assert renamed.status_code == 200 and b'value="invalid"' in renamed.content
    assert renamed.context["form"].errors and 'class="modal fade"' in renamed.content.decode()
    invalid = client.post(
        url(library, "upload"),
        {"pdfs": [pdf("accepted.pdf"), SimpleUploadedFile("bad.txt", b"bad")]},
    )
    assert invalid.status_code == 200 and b"bad.txt" in invalid.content
    assert library.entries.filter(name="accepted.pdf").exists()
    empty = client.post(url(library, "upload"))
    assert empty.status_code == 200 and empty.context["form"].errors
    tablet_for(scope, free=100, total=1000)
    accepted = client.post(url(library, "upload"), {"pdfs": [pdf("capacity.pdf")]})
    assert accepted.status_code == 302
    review = CapacityReview.objects.get(
        requested_by=actor,
        library=library,
        superseded_at=None,
    )
    assert review.publication_snapshot is not None and review.decision == "pending"
    assert library.entries.filter(name="capacity.pdf").exists()


def test_temporary_parent_and_shared_explorer_restrict_consume_and_expire(
    client, scope, pdf_pipeline
):
    admin, station_admin, _, _, _, station, _ = scope
    library = services.get_library(station_admin, station_id=station.id)
    document = services.upload_pdf(actor=station_admin, library_id=library.id, upload=pdf())
    access = services.activate_temporary_access(actor=admin, station=station)
    client.force_login(admin)
    parent = client.get(reverse("file-library", args=(library.id,)))
    html = parent.content.decode()
    assert "Open File Explorer" in html and "Temporary station access" in html
    for label in (
        "Choose station",
        "Publish Now",
        "Your capacity reviews",
        "Capacity reviews requiring approval",
    ):
        assert label not in html
    response = client.get(url(library))
    assert "file_library/explorer.html" in [template.name for template in response.templates]
    html = response.content.decode()
    for label in ("Preview", "Download", "Delete", "Release access", station.name):
        assert label in html
    for action in ("upload", "new-folder", "rename", "move", "replace"):
        assert url(library, action) not in html
        assert client.get(url(library, action), {"entry": document.id}).status_code == 403
        assert client.post(url(library, action), {"entry": document.id}).status_code == 403
    assert (
        client.post(reverse("file-library", args=(library.id,)), {"action": "publish"}).status_code
        == 403
    )
    failed = client.post(url(library, "delete"), {"entry": document.id, "confirm": "no"})
    assert failed.status_code == 200 and services.active_temporary_access(admin).id == access.id
    result = client.post(
        url(library, "delete"), {"entry": document.id, "confirm": "yes"}, HTTP_HX_REQUEST="true"
    )
    assert result.status_code == 204 and result["HX-Redirect"] == reverse("file-library-index")
    access.refresh_from_db()
    assert access.revoked_at is not None and not Entry.objects.filter(pk=document.pk).exists()
    assert client.get(url(library)).status_code == 403
    assert (
        client.get(reverse("file-library-document", args=(library.id, document.id))).status_code
        == 403
    )
    access = services.activate_temporary_access(actor=admin, station=station)
    access.activated_at = timezone.now() - timedelta(hours=1)
    access.expires_at = timezone.now() - timedelta(seconds=1)
    access.save(update_fields=("activated_at", "expires_at"))
    assert client.get(url(library)).status_code == 403
    access.refresh_from_db()
    assert access.revocation_reason == "expired"
    assert AuditEvent.objects.filter(
        action="file_library.temporary_access_expired", target_uuid=access.id
    ).exists()


def test_all_direct_routes_reauthorize_and_never_reveal_foreign_entries(
    client, scope, pdf_pipeline
):
    admin, station_admin, other, department, _, station, second = scope
    library = library_for(scope)
    document = services.upload_pdf(actor=admin, library_id=library.id, upload=pdf("secret.pdf"))
    system = User.objects.create_user("explorer-system@example.test", "System", "password")
    SystemRole.objects.create(user=system, role="SYSTEM_ADMIN", created_by=system)
    for actor in (station_admin, other, system):
        client.force_login(actor)
        assert client.get(url(library)).status_code == 403
        for action in ("upload", "new-folder", "rename", "move", "replace", "delete"):
            for response in (
                client.get(url(library, action), {"entry": document.id}),
                client.post(url(library, action), {"entry": document.id, "confirm": "yes"}),
            ):
                assert response.status_code == 403 and b"secret.pdf" not in response.content
        for suffix in ("", "?download=1"):
            assert (
                client.get(
                    reverse("file-library-document", args=(library.id, document.id)) + suffix
                ).status_code
                == 403
            )
        assert (
            client.get(reverse("file-library-station-search", args=(department.id,))).status_code
            == 403
        )
    station_library = services.get_library(station_admin, station_id=station.id)
    foreign_folder = services.create_folder(actor=admin, library_id=library.id, name="Foreign")
    client.force_login(station_admin)
    assert client.get(url(station_library), {"folder": foreign_folder.id}).status_code == 404
    assert client.get(url(station_library, "rename"), {"entry": document.id}).status_code == 404
    assert (
        client.post(
            url(station_library, "move"), {"entry": document.id, "destination": foreign_folder.id}
        ).status_code
        == 404
    )
    assert (
        client.get(reverse("file-library-station-access-choose", args=(library.id,))).status_code
        == 403
    )
    assert client.get(reverse("publications-list", args=(department.id,))).status_code == 403
    other_station = Library.objects.create(station=second)
    assert client.get(url(other_station)).status_code == 403
    StationAdminAssignment.objects.filter(user=station_admin).update(
        status="REVOKED", revoked_at=timezone.now(), revoked_by=admin
    )
    assert client.get(url(station_library)).status_code == 403
    client.force_login(admin)
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(admin)
    assert csrf.post(url(library, "new-folder"), {"name": "Denied"}).status_code == 403
    DepartmentMembership.objects.filter(user=admin).update(status="SUSPENDED")
    assert client.post(url(library, "new-folder"), {"name": "Denied"}).status_code == 403
    client.logout()
    assert client.get(url(library)).status_code == 302


def test_publication_relocation_and_requester_review_visibility(client, scope):
    admin, station_admin, _, department, _, station, _ = scope
    library = library_for(scope)
    station_library = services.get_library(station_admin, station_id=station.id)
    pending = review_for(station_admin, station_library)
    approved = review_for(
        station_admin,
        station_library,
        decision="approved",
        decided_by=admin,
        decided_at=timezone.now(),
    )
    private = review_for(admin, station_library, publication_snapshot=None)
    expired = review_for(
        station_admin, station_library, expires_at=timezone.now() - timedelta(seconds=1)
    )
    client.force_login(admin)
    assert (
        client.post(reverse("file-library", args=(library.id,)), {"action": "publish"}).status_code
        == 403
    )
    page = client.get(reverse("file-library", args=(library.id,)))
    assert reverse("file-library-capacity-review", args=(pending.id,)) in page.content.decode()
    scope_state = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_files"
    )
    detail = client.get(reverse("publications-scope-detail", args=(scope_state.id,)))
    assert detail.status_code == 200
    assert (
        reverse("publications-department-files-publish", args=(department.id,))
        not in detail.content.decode()
    )
    assert "Publish Now" not in detail.content.decode()
    assert "Build now" in detail.content.decode()
    client.force_login(station_admin)
    page = client.get(reverse("file-library", args=(station_library.id,)))
    html = page.content.decode()
    for review in (pending, approved):
        assert reverse("file-library-capacity-review", args=(review.id,)) in html
    for review in (private, expired):
        assert reverse("file-library-capacity-review", args=(review.id,)) not in html
    assert "Your capacity reviews" in html and "Publish Now" in html
    # The synthetic snapshots do not match the current candidate. A fresh
    # assessment clears them; historical decisions cannot stay actionable.
    pending.refresh_from_db()
    approved.refresh_from_db()
    assert pending.superseded_at and approved.superseded_at
    assert "Superseded" in html
    assert (
        client.post(
            reverse("file-library-capacity-review", args=(pending.id,)),
            {"action": "approve", "confirm": "yes"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            reverse("file-library", args=(station_library.id,)), {"action": "publish"}
        ).status_code
        == 302
    )
    assert PublicationJob.objects.filter(station=station, requested_by=station_admin).exists()


def test_station_dialog_bounded_search_fallback_reauth_and_stale_choice(client, scope):
    admin, station_admin, _, department, outside, station, _ = scope
    library = library_for(scope)
    from apps.organizations.models import Station

    Station.objects.bulk_create(
        [
            Station(department=department, name=f"Similar {i:03}", short_code=f"S{i:03}")
            for i in range(120)
        ]
    )
    Station.objects.create(department=outside, name="Foreign station", short_code="FOREIGN")
    inactive = Station.objects.create(
        department=department, name="Inactive station", short_code="OFF", active=False
    )
    client.force_login(admin)
    choose = reverse("file-library-station-access-choose", args=(library.id,))
    response = client.get(choose, HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert 'role="combobox"' in html and 'aria-controls="station-listbox"' in html
    assert 'id="station-listbox"' in html and 'id="station-selection-status"' in html
    assert len(response.context["station_access_form"].options) == 25
    assert "S119" not in html
    endpoint = reverse("file-library-station-search", args=(department.id,))
    result = client.get(endpoint, {"q": "Similar"})
    assert result.content.decode().count('role="option"') == 20
    assert (
        "Foreign station" not in result.content.decode()
        and "Inactive station" not in result.content.decode()
    )
    assert "No matching active stations" in client.get(endpoint, {"q": "no match"}).content.decode()
    result = client.get(choose, {"station_q": "S119"})
    assert len(result.context["station_access_form"].options) == 1 and b"S119" in result.content
    post = reverse("file-library-station-access-select")
    assert client.post(
        post, {"department_id": department.id, "station_id": station.id, "confirm": "yes"}
    ).url.startswith(reverse("accounts-reauthenticate"))
    assert choose in client.session["pending_reauth"]["return_url"]
    reauth(client)
    result = client.post(
        post,
        {"department_id": department.id, "station_id": station.id, "confirm": "yes"},
        HTTP_HX_REQUEST="true",
    )
    assert result.status_code == 204 and result["HX-Redirect"] == reverse(
        "file-library", args=(Library.objects.get(station=station).id,)
    )
    result = client.post(
        post,
        {"department_id": department.id, "station_id": inactive.id, "confirm": "yes"},
        HTTP_HX_REQUEST="true",
    )
    assert result.status_code == 200 and result.context["station_access_form"].errors
    client.force_login(station_admin)
    assert client.get(choose).status_code == 403 and client.get(endpoint).status_code == 403


@pytest.mark.parametrize("selector", ["temporary_access", "publication_filter"])
def test_station_combobox_keyboard_pointer_and_stale_responses_in_browser(
    tmp_path, client, scope, selector
):
    if sys.platform == "win32":
        pytest.skip(
            "Native Linux browser check: this Windows sandbox cannot launch Edge's GPU process."
        )
    browser = shutil.which("google-chrome") or shutil.which("chromium")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not browser and edge.exists():
        browser = str(edge)
    if not browser:
        pytest.skip(
            "Requires an installed Chrome, Chromium or Edge browser for DOM interaction tests."
        )
    source = Path("static/file_library/station_access.js").read_text(encoding="utf-8")
    # Real browser DOM/events; HTTP authorization and bounded result sets are tested above.
    library = library_for(scope)
    client.force_login(scope[0])
    if selector == "publication_filter":
        html = client.get(
            reverse("publications-list", args=(scope[3].id,)), {"scope": "station"}
        ).content.decode()
    else:
        html = client.get(
            reverse("file-library-station-access-choose", args=(library.id,))
        ).content.decode()
    html += '<pre id="result">WAIT</pre><script>window.htmx={};</script>'

    driver = (
        Path(__file__)
        .with_name("fixtures")
        .joinpath("station_combobox_browser.js")
        .read_text(encoding="utf-8")
    )
    fixture = tmp_path / "combobox.html"
    fixture.write_text(
        html + "<script>" + source + "</script><script>" + driver + "</script>", encoding="utf-8"
    )
    result = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={tmp_path / 'browser'}",
            "--dump-dom",
            fixture.as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert '<pre id="result">PASS</pre>' in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("role", ["department", "station"])
def test_library_rename_overview_form_and_authorization(client, scope, role):
    actor, library = normal(scope, role)
    parent_url = reverse("file-library", args=(library.id,))
    client.force_login(actor)
    library.refresh_from_db()
    revision, usage, original_name = library.source_revision, library.usage_bytes, library.name
    overview = client.get(parent_url)
    assert 'aria-label="Rename library"' in overview.content.decode()
    assert f'href="{parent_url}?rename=1#library-rename"' in overview.content.decode()
    edit = client.get(parent_url, {"rename": "1"})
    html = edit.content.decode()
    assert 'id="library-rename" method="post"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert f'value="{original_name}"' in html
    assert '<label class="form-label" for="library-name">Library name</label>' in html
    assert ">Save</button>" in html and f'href="{parent_url}">Cancel</a>' in html
    assert 'name="action" value="library_name"' not in client.get(url(library)).content.decode()
    for invalid in ("", "../invalid", "x" * 256):
        response = client.post(parent_url, {"action": "library_name", "name": invalid})
        assert response.status_code == 200
        assert 'id="library-name-error"' in response.content.decode()
        assert 'aria-invalid="true"' in response.content.decode()
        assert f'value="{invalid}"' in response.content.decode()
        library.refresh_from_db()
        assert library.name == original_name and library.source_revision == revision
    response = client.post(
        parent_url, {"action": "library_name", "name": "New library"}, follow=True
    )
    assert response.status_code == 200 and b"Library renamed." in response.content
    assert response.redirect_chain == [(parent_url, 302)]
    library.refresh_from_db()
    assert library.name == "New library" and library.source_revision == revision + 1
    assert library.usage_bytes == usage
    event = AuditEvent.objects.get(action="file_library.library_renamed", target_uuid=library.id)
    assert event.actor_user_id == actor.id and event.station_id == library.station_id
    assert not library.entries.exists()
    # Saving the same name remains a no-op in the authoritative service.
    client.post(parent_url, {"action": "library_name", "name": "New library"})
    library.refresh_from_db()
    assert library.source_revision == revision + 1
    assert AuditEvent.objects.filter(action="file_library.library_renamed").count() == 1
    assert (
        Client(enforce_csrf_checks=True).post(parent_url, {"action": "library_name"}).status_code
        == 403
    )
    client.force_login(scope[2])
    assert client.get(parent_url, {"rename": "1"}).status_code == 403
    assert client.post(parent_url, {"action": "library_name", "name": "Foreign"}).status_code == 403
    client.force_login(scope[1] if role == "department" else scope[0])
    assert (
        client.post(parent_url, {"action": "library_name", "name": "Wrong scope"}).status_code
        == 403
    )
    library.refresh_from_db()
    assert library.name == "New library" and library.source_revision == revision + 1


def test_temporary_access_cannot_rename_library(client, scope):
    actor, library = normal(scope, "station")
    grant = services.activate_temporary_access(actor=scope[0], station=scope[5])
    client.force_login(scope[0])
    library.refresh_from_db()
    original_name, revision = library.name, library.source_revision
    for query in ({}, {"rename": "1"}):
        response = client.get(reverse("file-library", args=(library.id,)), query)
        assert response.status_code == 200
        html = response.content.decode()
        assert 'aria-label="Rename library"' not in html and 'id="library-rename"' not in html
    assert (
        client.post(
            reverse("file-library", args=(library.id,)),
            {"action": "library_name", "name": "Denied"},
        ).status_code
        == 403
    )
    library.refresh_from_db()
    grant.refresh_from_db()
    assert library.name == original_name and library.source_revision == revision
    assert grant.revoked_at is None
    assert not AuditEvent.objects.filter(action="file_library.library_renamed").exists()


@pytest.mark.parametrize("role", ["department", "station"])
def test_move_destination_presentation_and_navigation(client, scope, role):
    actor, library = normal(scope, role)
    item = services.create_folder(actor=actor, library_id=library.id, name="Test")
    plans = services.create_folder(actor=actor, library_id=library.id, name="KLGV Plans")
    child = services.create_folder(
        actor=actor, library_id=library.id, name="Subfolder", parent_id=plans.id
    )
    client.force_login(actor)
    for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
        root = client.get(url(library, "move"), {"entry": item.id}, **headers)
        assert root.status_code == 200
        assert (
            '<strong>Destination:</strong> <em class="text-body-secondary">Files</em>'
            in root.content.decode()
        )
        assert 'aria-label="Destination pages"' not in root.content.decode()
        response = client.get(
            url(library, "move"), {"entry": item.id, "destination": child.id}, **headers
        )
        html = response.content.decode()
        assert "<strong>Test</strong>" in html
        assert (
            '<strong>Destination:</strong> <em class="text-body-secondary">'
            "Files / KLGV Plans / Subfolder</em>" in html
        )
        assert "The selected item will be moved here when you confirm." in html
        assert ">Up one level</a>" in html and ">Files</a>" in html
        assert f"destination={plans.id}" in html
        assert 'name="destination"' in html and f'value="{child.id}"' in html
        assert "Page 1 of 1" not in html and 'aria-label="Destination pages"' not in html
        assert ">Move here</button>" in html
        assert ("hx-get=" in html) == bool(headers)
        assert ("hx-post=" in html) == bool(headers)
    for index in range(26):
        services.create_folder(
            actor=actor, library_id=library.id, name=f"Folder {index}", parent_id=plans.id
        )
    page = client.get(
        url(library, "move"), {"entry": item.id, "destination": plans.id}, HTTP_HX_REQUEST="true"
    )
    assert 'aria-label="Destination pages"' in page.content.decode()
    assert "Page 1 of 2" in page.content.decode()
    assert page.context["destinations"].has_next()
    response = client.post(url(library, "move"), {"entry": item.id, "destination": child.id})
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.parent_id == child.id


@pytest.mark.parametrize("role", ["department", "station"])
@pytest.mark.parametrize("action", ["rename", "move", "delete"])
def test_actions_reject_missing_or_invalid_entries(client, scope, role, action):
    actor, library = normal(scope, role)
    deleted = services.create_folder(actor=actor, library_id=library.id, name="Deleted")
    services.delete_entry(actor=actor, library_id=library.id, entry_id=deleted.id)
    library.refresh_from_db()
    revision = library.source_revision
    events = AuditEvent.objects.count()
    client.force_login(actor)
    for entry_id in (None, "invalid", "00000000-0000-0000-0000-000000000001", str(deleted.id)):
        data = {"name": "New name", "confirm": "yes", "destination": ""}
        if entry_id is not None:
            data["entry"] = entry_id
        for method in (client.get, client.post):
            for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
                response = method(url(library, action), data, **headers)
                assert response.status_code == 404
    library.refresh_from_db()
    assert library.source_revision == revision
    assert AuditEvent.objects.count() == events
    assert not library.entries.exists()
