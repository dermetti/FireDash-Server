import io
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import cast

import pikepdf
import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.file_library import services
from apps.file_library.models import Content, Entry, Library
from apps.organizations.models import Department, Station
from apps.reference_data.pdf_sandbox import PdfSanitizerError
from apps.reference_data.pdf_validation import PdfValidationError

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    admin = User.objects.create_user("library@example.test", "Admin", "password")
    station_admin = User.objects.create_user("station@example.test", "Station", "password")
    other = User.objects.create_user("other@example.test", "Other", "password")
    department = Department.objects.create(name="Alpha", short_code="A", created_by=admin)
    outside = Department.objects.create(name="Bravo", short_code="B", created_by=other)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(user=other, department=outside, created_by=other)
    station = Station.objects.create(department=department, name="One", short_code="ONE")
    second = Station.objects.create(department=department, name="Two", short_code="TWO")
    StationAdminAssignment.objects.create(user=station_admin, station=station, created_by=admin)
    return admin, station_admin, other, department, outside, station, second


@pytest.fixture
def pdf_pipeline(settings, tmp_path, monkeypatch):
    settings.REFERENCE_DATA_QUARANTINE_ROOT = tmp_path / "quarantine"
    settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT = tmp_path / "output"
    settings.REFERENCE_DATA_ACCEPTED_ROOT = tmp_path / "accepted"
    settings.REFERENCE_DATA_ACCEPTED_GROUP = ""
    # Windows lacks libmagic and the Linux broker. Keep real pikepdf structure,
    # encryption, size, signature and digest validation; stub these platform boundaries.
    monkeypatch.setitem(
        sys.modules, "magic", SimpleNamespace(from_file=lambda *a, **kw: "application/pdf")
    )
    monkeypatch.setattr(
        services.pdf_sandbox,
        "sanitize",
        lambda quarantined_input, sanitized_output: shutil.copyfile(
            quarantined_input, sanitized_output
        ),
    )
    return tmp_path


def pdf(name="guide.pdf", pages=1):
    output = io.BytesIO()
    with pikepdf.new() as document:
        for _ in range(pages):
            document.add_blank_page()
        document.save(output)
    return SimpleUploadedFile(name, output.getvalue(), content_type="application/pdf")


def library_for(scope):
    return services.get_library(scope[0], department_id=scope[3].id)


def test_scope_isolation_and_role_precedence(scope):
    admin, station_admin, other, department, outside, station, second = scope
    department_library = library_for(scope)
    station_library = services.get_library(station_admin, station_id=station.id)
    for actor, library in [
        (admin, station_library),
        (station_admin, department_library),
        (other, department_library),
        (other, station_library),
    ]:
        with pytest.raises(PermissionDenied):
            services.require_library(actor, library)
    for station_id in [
        second.id,
        Station.objects.create(department=outside, name="Outside", short_code="OUT").id,
    ]:
        with pytest.raises(PermissionDenied):
            services.get_library(station_admin, station_id=station_id)
    StationAdminAssignment.objects.create(user=admin, station=station, created_by=admin)
    with pytest.raises(PermissionDenied):
        services.require_library(admin, station_library)
    SystemRole.objects.create(user=other, created_by=admin)
    with pytest.raises(PermissionDenied):
        services.get_library(other, department_id=outside.id)
    DepartmentMembership.objects.filter(user=admin).update(status="SUSPENDED")
    with pytest.raises(PermissionDenied):
        services.require_library(admin, department_library)


def test_upload_replace_delete_revision_accounting_and_gc(scope, pdf_pipeline):
    admin = scope[0]
    library = library_for(scope)
    document = services.upload_pdf(actor=admin, library_id=library.id, upload=pdf())
    old_content = document.content
    old_path = services.content_path(old_content)
    assert not services.collect_content(old_content.id)
    replacement = services.upload_pdf(
        actor=admin, library_id=library.id, upload=pdf("new.pdf", pages=2), replace_id=document.id
    )
    assert replacement.id == document.id and replacement.name == "guide.pdf"
    assert replacement.content_id != old_content.id and old_path.exists()
    library.refresh_from_db()
    assert library.usage_bytes == replacement.content.size_bytes
    assert library.source_revision == 2
    services.delete_entry(actor=admin, library_id=library.id, entry_id=document.id)
    library.refresh_from_db()
    assert library.usage_bytes == 0 and library.source_revision == 3
    assert Library.objects.filter(pk=library.pk).exists() and not Entry.objects.exists()
    assert Content.objects.count() == 2
    assert services.collect_content(old_content.id)
    events = AuditEvent.objects.filter(action__startswith="file_library.")
    assert set(events.values_list("action", flat=True)) == {
        "file_library.uploaded",
        "file_library.replaced",
        "file_library.deleted",
    }
    assert all(
        event.actor_user_id == admin.id
        and event.department_id == scope[3].id
        and event.target_uuid == document.id
        for event in events
    )


@pytest.mark.parametrize(
    "failure",
    [
        "extension",
        "signature",
        "malformed",
        "encrypted",
        "sanitizer",
        "output",
        "quota",
        "size",
        "audit",
    ],
)
def test_rejected_uploads_leave_no_canonical_state(
    scope, pdf_pipeline, monkeypatch, settings, failure
):
    library = library_for(scope)
    upload = pdf()
    expected = (services.LibraryError, PdfValidationError, PdfSanitizerError, RuntimeError)
    if failure == "extension":
        upload.name = "bad.txt"
    elif failure in {"signature", "malformed"}:
        upload = SimpleUploadedFile(
            "bad.pdf", b"%PDF-1.7\nbad" if failure == "malformed" else b"bad"
        )
    elif failure == "encrypted":
        output = io.BytesIO()
        with pikepdf.new() as doc:
            doc.add_blank_page()
            doc.save(output, encryption=pikepdf.Encryption(owner="secret", user="secret"))
        upload = SimpleUploadedFile("encrypted.pdf", output.getvalue())
    elif failure == "sanitizer":

        def fail(**kwargs):
            raise PdfSanitizerError("Unavailable")

        monkeypatch.setattr(services.pdf_sandbox, "sanitize", fail)
    elif failure == "output":
        monkeypatch.setattr(
            services.pdf_sandbox,
            "sanitize",
            lambda **kwargs: kwargs["sanitized_output"].write_bytes(b"bad"),
        )
    elif failure == "quota":
        Library.objects.filter(pk=library.pk).update(quota_bytes=1)
    elif failure == "size":
        settings.FILE_LIBRARY_MAX_FILE_BYTES = 1
    elif failure == "audit":

        def fail(**kwargs):
            raise RuntimeError("Audit failure")

        monkeypatch.setattr(services, "record_event", fail)
    with pytest.raises(expected):
        services.upload_pdf(actor=scope[0], library_id=library.id, upload=upload)
    library.refresh_from_db()
    assert library.source_revision == library.usage_bytes == 0
    assert not Entry.objects.exists() and not Content.objects.exists()
    assert not list(pdf_pipeline.rglob("*.pdf"))


def test_folder_integrity_moves_and_recursive_delete(scope, pdf_pipeline):
    library = library_for(scope)
    common = {"actor": scope[0], "library_id": library.id}
    root = services.create_folder(**common, name="Root")
    parent = root
    for i in range(4):
        parent = services.create_folder(**common, name=str(i), parent_id=parent.id)
    with pytest.raises(services.LibraryError):
        services.create_folder(**common, name="Six", parent_id=parent.id)
    with pytest.raises(services.LibraryError):
        services.create_folder(**common, name="ROOT")
    with pytest.raises(services.LibraryError):
        services.edit_entry(**common, entry_id=root.id, parent_id=parent.id, move=True)
    other = services.create_folder(**common, name="Other")
    with pytest.raises(services.LibraryError):
        services.edit_entry(**common, entry_id=root.id, parent_id=other.id, move=True)
    document = services.upload_pdf(**common, upload=pdf(), parent_id=parent.id)
    services.edit_entry(**common, entry_id=document.id, parent_id=other.id, move=True)
    services.edit_entry(**common, entry_id=document.id, name="renamed.pdf")
    services.delete_entry(**common, entry_id=root.id)
    assert Entry.objects.count() == 2
    services.delete_entry(**common, entry_id=other.id)
    library.refresh_from_db()
    assert library.usage_bytes == 0 and not Entry.objects.exists()


def test_cross_library_targets_and_database_guards(scope, pdf_pipeline):
    library = library_for(scope)
    foreign = services.get_library(scope[2], department_id=scope[4].id)
    folder = services.create_folder(actor=scope[2], library_id=foreign.id, name="Foreign")
    local = services.create_folder(actor=scope[0], library_id=library.id, name="Local")
    common = {"actor": scope[0], "library_id": library.id}
    for operation in [
        lambda: services.create_folder(**common, name="bad", parent_id=folder.id),
        lambda: services.edit_entry(**common, entry_id=folder.id, name="bad"),
        lambda: services.delete_entry(**common, entry_id=folder.id),
        lambda: services.upload_pdf(**common, upload=pdf(), replace_id=folder.id),
    ]:
        with pytest.raises(services.LibraryError):
            operation()
    content = services.upload_pdf(**common, upload=pdf()).content
    for operation in [
        lambda: Entry.objects.filter(pk=local.pk).update(parent=folder),
        lambda: Entry.objects.filter(pk=local.pk).update(parent=local),
        lambda: Entry.objects.filter(pk=local.pk).update(library=foreign),
        lambda: Content.objects.filter(pk=content.pk).update(sha256="0" * 64),
        lambda: Library.objects.filter(pk=library.pk).update(station=scope[5]),
        lambda: Entry.objects.create(
            library=library, name="LOCAL", is_folder=True, modified_by=scope[0]
        ),
    ]:
        with pytest.raises(DatabaseError), transaction.atomic():
            operation()


def test_portal_bulk_errors_preview_search_and_confirmation(scope, pdf_pipeline, client):
    library = library_for(scope)
    client.force_login(scope[0])
    url = reverse("file-library", args=[library.id])
    result = client.post(
        url,
        {"action": "upload", "pdfs": [pdf(), SimpleUploadedFile("bad.txt", b"bad")]},
        follow=True,
    )
    assert result.status_code == 200
    assert b"Uploaded guide.pdf" in result.content and b"must end in .pdf" in result.content
    document = Entry.objects.get()
    doc_url = reverse("file-library-document", args=[library.id, document.id])
    for suffix in ["", "?download=1"]:
        response = client.get(doc_url + suffix)
        assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
        assert b"%PDF" in b"".join(response.streaming_content)
    assert b"guide.pdf" in client.get(url + "?q=guide").content
    client.post(url, {"action": "library_name", "name": "Operations"})
    library.refresh_from_db()
    assert library.name == "Operations"
    client.post(url, {"action": "delete", "entry": document.id})
    assert Entry.objects.filter(pk=document.pk).exists()
    assert client.get(url + "?folder=invalid").status_code == 404
    for actor in [scope[1], scope[2]]:
        client.force_login(actor)
        assert client.get(url).status_code == client.get(doc_url).status_code == 403
        for action in ["upload", "replace", "rename", "move", "delete", "folder", "library_name"]:
            assert client.post(url, {"action": action, "entry": document.id}).status_code == 403


def test_data_hub_role_tiles(scope, client):
    client.force_login(scope[0])
    assert b"Dateiverwaltung" in client.get(reverse("portal-data-hub", args=[scope[3].id])).content
    station_hub = reverse("station-data-hub", args=[scope[5].id])
    assert client.get(station_hub).status_code == 403
    client.force_login(scope[1])
    assert b"Dateiverwaltung" in client.get(station_hub).content
    assert client.get(reverse("portal-data-hub", args=[scope[3].id])).status_code == 403
    assert client.get(reverse("file-library-index")).status_code == 302


@pytest.mark.django_db(transaction=True)
def test_concurrent_uploads_enforce_capacity(scope, pdf_pipeline, monkeypatch):
    assert connection.vendor == "postgresql"
    library = library_for(scope)
    Library.objects.filter(pk=library.pk).update(quota_bytes=pdf().size)
    barrier = Barrier(2)
    original = services.pdf_sandbox.sanitize

    def sanitize(**kwargs):
        original(**kwargs)
        barrier.wait(timeout=10)

    monkeypatch.setattr(services.pdf_sandbox, "sanitize", sanitize)

    def upload(index):
        close_old_connections()
        try:
            services.upload_pdf(actor=scope[0], library_id=library.id, upload=pdf(f"{index}.pdf"))
            return "ok"
        except services.LibraryError:
            return "quota"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(upload, range(2))) == ["ok", "quota"]
    library.refresh_from_db()
    assert library.source_revision == 1 and library.usage_bytes == Content.objects.get().size_bytes


def test_replacement_quota_and_audit_failure_preserve_old_content(scope, pdf_pipeline, monkeypatch):
    library = library_for(scope)
    common = {"actor": scope[0], "library_id": library.id}
    document = services.upload_pdf(**common, upload=pdf())
    original_id = document.content_id
    Library.objects.filter(pk=library.pk).update(quota_bytes=document.content.size_bytes)
    with pytest.raises(services.LibraryError, match="quota"):
        services.upload_pdf(**common, upload=pdf(pages=3), replace_id=document.id)
    Library.objects.filter(pk=library.pk).update(quota_bytes=10_000)

    def fail(**kwargs):
        raise RuntimeError("Audit failure")

    monkeypatch.setattr(services, "record_event", fail)
    with pytest.raises(RuntimeError, match="Audit"):
        services.upload_pdf(**common, upload=pdf(pages=3), replace_id=document.id)
    with pytest.raises(RuntimeError, match="Audit"):
        services.delete_entry(**common, entry_id=document.id)
    with pytest.raises(RuntimeError, match="Audit"):
        services.rename_library(**common, name="Failed rename")
    library.refresh_from_db()
    document.refresh_from_db()
    assert document.content_id == original_id and Content.objects.count() == 1
    assert library.source_revision == 1 and library.usage_bytes == document.content.size_bytes
    assert library.name == "Documents" and len(list(pdf_pipeline.rglob("*.pdf"))) == 1


@pytest.mark.parametrize("kind", ["mime", "empty", "active", "stream_limit", "promotion"])
def test_additional_ingestion_failures(scope, pdf_pipeline, monkeypatch, settings, kind):
    library = library_for(scope)
    upload = pdf()
    expected: tuple[type[Exception], ...] = (PdfValidationError,)
    if kind == "mime":
        monkeypatch.setitem(
            sys.modules, "magic", SimpleNamespace(from_file=lambda *a, **kw: "text/plain")
        )
    elif kind == "empty":
        upload = pdf(pages=0)
    elif kind == "active":
        output = io.BytesIO()
        with pikepdf.new() as doc:
            doc.add_blank_page()
            doc.Root["/OpenAction"] = pikepdf.Dictionary(
                S=pikepdf.Name("/JavaScript"), JS="app.alert('unsafe')"
            )
            doc.save(output)
        upload = SimpleUploadedFile("active.pdf", output.getvalue())
    elif kind == "stream_limit":
        from apps.reference_data.storage import StorageError

        expected = (StorageError,)
        settings.FILE_LIBRARY_MAX_FILE_BYTES = 10
        upload.size = 1  # The streaming limit must not trust declared upload size.
    elif kind == "promotion":
        expected = (OSError,)
        original = services.storage.promote_to_accepted

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Permission failure after promotion")

        monkeypatch.setattr(services.storage, "promote_to_accepted", fail)
    with pytest.raises(expected):
        services.upload_pdf(actor=scope[0], library_id=library.id, upload=upload)
    assert not Entry.objects.exists() and not Content.objects.exists()
    assert not list(pdf_pipeline.rglob("*.pdf"))


def test_station_operations_audit_scope_and_cross_station_rejection(scope, pdf_pipeline, client):
    actor, station = scope[1], scope[5]
    library = services.get_library(actor, station_id=station.id)
    client.force_login(actor)
    url = reverse("file-library", args=[library.id])
    for data in [
        {"action": "folder", "name": "Manuals"},
        {"action": "library_name", "name": "Station manuals"},
    ]:
        assert client.post(url, data, follow=True).status_code == 200
    folder = Entry.objects.get(is_folder=True)
    assert (
        client.post(url + f"?folder={folder.id}", {"action": "upload", "pdfs": [pdf()]}).status_code
        == 302
    )
    document = Entry.objects.get(is_folder=False)
    for data in [
        {"action": "rename", "entry": document.id, "name": "Operations.pdf"},
        {"action": "move", "entry": document.id, "destination": ""},
        {"action": "replace", "entry": document.id, "pdf": pdf(pages=2)},
    ]:
        client.post(url, data)
    document.refresh_from_db()
    assert document.name == "Operations.pdf" and document.parent_id is None
    assert b"Manuals" in client.get(url + "?q=manual").content
    doc_url = reverse("file-library-document", args=[library.id, document.id])
    response = client.get(doc_url)
    assert response.status_code == 200
    assert b"%PDF" in b"".join(response.streaming_content)
    services.content_path(document.content).write_bytes(b"corrupted")
    assert client.get(doc_url).status_code == 404
    client.post(url, {"action": "delete", "entry": document.id, "confirm": "yes"})
    events = AuditEvent.objects.filter(action__startswith="file_library.")
    assert events.count() == 7
    assert all(
        event.station_id == station.id
        and event.department_id == station.department_id
        and event.actor_user_id == actor.id
        for event in events
    )
    StationAdminAssignment.objects.filter(user=actor).update(station=scope[6])
    assert client.get(url).status_code == client.get(doc_url).status_code == 403


def test_shared_content_retained_until_last_reference_and_commit(
    scope, pdf_pipeline, django_capture_on_commit_callbacks
):
    library = library_for(scope)
    common = {"actor": scope[0], "library_id": library.id}
    document = services.upload_pdf(**common, upload=pdf())
    # A second protected reference models reuse; production source changes use services.
    duplicate = Entry.objects.create(
        library=library, name="second.pdf", content=document.content, modified_by=scope[0]
    )
    path = services.content_path(document.content)
    document.delete()
    assert services.collect_content(duplicate.content_id) is False and path.exists()
    content_id = duplicate.content_id
    duplicate.delete()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.collect_content(content_id)
        assert path.exists()  # physical removal must wait for commit
    assert not path.exists()


def test_database_depth_content_namespace_and_revision_guards(scope, pdf_pipeline):
    library = library_for(scope)
    common = {"actor": scope[0], "library_id": library.id}
    folders: list[Entry] = []
    for index in range(5):
        folders.append(
            services.create_folder(
                **common, name=str(index), parent_id=folders[-1].id if folders else None
            )
        )
    other = services.create_folder(**common, name="other")
    doc = services.upload_pdf(**common, upload=pdf())
    foreign = services.get_library(scope[2], department_id=scope[4].id)
    for operation in [
        lambda: Entry.objects.create(
            library=library, parent=folders[-1], name="six", is_folder=True, modified_by=scope[0]
        ),
        lambda: Entry.objects.filter(pk=folders[0].pk).update(parent=other),
        lambda: Entry.objects.filter(pk=other.pk).update(parent=doc),
        lambda: Entry.objects.create(
            library=foreign, name="cross.pdf", content=doc.content, modified_by=scope[2]
        ),
        lambda: Entry.objects.create(
            library=library, name="GUIDE.PDF", is_folder=True, modified_by=scope[0]
        ),
        lambda: Library.objects.filter(pk=library.pk).update(source_revision=0),
    ]:
        with pytest.raises(DatabaseError), transaction.atomic():
            operation()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("race", ["replacements", "delete_replace"])
def test_concurrent_capacity_changes(scope, pdf_pipeline, monkeypatch, race):
    library = library_for(scope)
    common = {"actor": scope[0], "library_id": library.id}
    first = services.upload_pdf(**common, upload=pdf("one.pdf"))
    second = services.upload_pdf(**common, upload=pdf("two.pdf"))
    original_usage = first.content.size_bytes + second.content.size_bytes
    new_size = pdf(pages=3).size
    Library.objects.filter(pk=library.pk).update(
        quota_bytes=original_usage + new_size - first.content.size_bytes
    )
    barrier = Barrier(2)
    original = services.pdf_sandbox.sanitize

    def sanitize(**kwargs):
        original(**kwargs)
        barrier.wait(timeout=10)

    monkeypatch.setattr(services.pdf_sandbox, "sanitize", sanitize)

    def mutate(index):
        close_old_connections()
        try:
            if race == "delete_replace" and index == 0:
                barrier.wait(timeout=10)
                services.delete_entry(**common, entry_id=first.id)
            else:
                services.upload_pdf(
                    **common, upload=pdf(pages=3), replace_id=first.id if index == 0 else second.id
                )
            return "ok"
        except services.LibraryError:
            return "quota"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(mutate, range(2)))
    assert results == (["ok", "quota"] if race == "replacements" else ["ok", "ok"])
    library.refresh_from_db()
    actual = sum(
        cast(Content, entry.content).size_bytes for entry in Entry.objects.select_related("content")
    )
    assert library.usage_bytes == actual <= library.quota_bytes
    assert library.source_revision == (3 if race == "replacements" else 4)


@pytest.mark.django_db(transaction=True)
def test_migrations_reverse_and_reapply():
    executor = MigrationExecutor(connection)
    target = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("file_library", None)])
        assert "file_library_library" not in connection.introspection.table_names()
        MigrationExecutor(connection).migrate([("publications", "0027_publication_scope_types")])
        from apps.publications.models import DatasetTypeRegistry

        assert not DatasetTypeRegistry.objects.filter(
            code__in=("department_files", "station_files")
        ).exists()
    finally:
        MigrationExecutor(connection).migrate(target)
    assert "file_library_library" in connection.introspection.table_names()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'file_library_%'")
        assert cursor.fetchone()[0] >= 6
