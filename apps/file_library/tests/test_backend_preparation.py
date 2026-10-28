# ruff: noqa: F811
"""Phase 1 contracts: decimal policies, bounded reads and publication parity."""

import importlib
import runpy
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import cast
from unittest.mock import Mock

import pytest
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage
from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.file_library import services
from apps.file_library.explorer import Sort, read_explorer
from apps.file_library.models import Content, DepartmentQuotaPolicy, Entry, Library, default_quota
from apps.file_library.publication import initialize_missing_libraries
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetScopeState, PublicationJob

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "legacy,department,station,expected",
    [
        (None, None, None, (5_000_000_000, 2_000_000_000)),
        ("1234567", None, None, (1234567, 1234567)),
        ("1073741824", "5000000000", None, (5_000_000_000, 1_073_741_824)),
        (None, "333", "222", (333, 222)),
    ],
)
def test_quota_setting_environment_compatibility(
    monkeypatch, legacy, department, station, expected
):
    for key, value in (
        ("FILE_LIBRARY_QUOTA_BYTES", legacy),
        ("FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES", department),
        ("FILE_LIBRARY_STATION_QUOTA_BYTES", station),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    configured = runpy.run_path("config/settings/base.py")
    assert (
        configured["FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES"],
        configured["FILE_LIBRARY_STATION_QUOTA_BYTES"],
    ) == expected
    assert configured["FILE_LIBRARY_QUOTA_BYTES"] == int(legacy or "1073741824")


def test_defaults_shared_future_stations_worker_and_isolation(scope, settings):
    admin, station_admin, other, department, outside, station, second = scope
    assert default_quota() == 1_073_741_824  # Historical migration callable stays intact.
    initialize_missing_libraries()
    department_library = Library.objects.get(department=department)
    station_library = Library.objects.get(station=station)
    assert department_library.quota_bytes == 5_000_000_000
    assert station_library.quota_bytes == 2_000_000_000
    assert Library.objects.get(station=second).quota_bytes == 2_000_000_000
    services.set_department_quotas(actor=admin, department=department, station_files_bytes=1234567)
    future = Station.objects.create(department=department, name="Future", short_code="FUT")
    initialize_missing_libraries()
    assert Library.objects.get(station=future).quota_bytes == 1234567
    assert station_library.quota_bytes == 1234567
    assert department_library.quota_bytes == 5_000_000_000
    assert Library.objects.get(department=outside).quota_bytes == 5_000_000_000
    settings.FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES = 444
    settings.FILE_LIBRARY_STATION_QUOTA_BYTES = 222
    policy = DepartmentQuotaPolicy(department=department)
    assert (policy.department_files_bytes, policy.station_files_bytes) == (444, 222)
    assert default_quota() == 1_073_741_824


def test_quota_enforcement_uses_shared_bytes_without_source_changes(scope, pdf_pipeline):
    admin, station_admin, _, department, _, station, _ = scope
    library = services.get_library(station_admin, station_id=station.id)
    size = len(pdf().read())
    before = (library.source_revision, library.usage_bytes, PublicationJob.objects.count())
    services.set_department_quotas(actor=admin, department=department, station_files_bytes=size - 1)
    with pytest.raises(services.LibraryError, match="quota"):
        services.upload_pdf(actor=station_admin, library_id=library.id, upload=pdf())
    library.refresh_from_db()
    assert (library.source_revision, library.usage_bytes, PublicationJob.objects.count()) == before
    services.set_department_quotas(actor=admin, department=department, station_files_bytes=size)
    entry = services.upload_pdf(actor=station_admin, library_id=library.id, upload=pdf())
    assert entry.content.size_bytes == size
    library.refresh_from_db()
    assert library.usage_bytes == size and library.source_revision == before[0] + 1


@pytest.fixture
def explorer_tree(scope):
    admin = scope[0]
    library = library_for(scope)
    root = services.create_folder(actor=admin, library_id=library.id, name="Zulu")
    child = services.create_folder(
        actor=admin, library_id=library.id, name="Child", parent_id=root.id
    )
    services.create_folder(actor=admin, library_id=library.id, name="Alpha")
    files = []
    for name, size, parent in (("b.pdf", 10, None), ("A.pdf", 30, None), ("Needle.pdf", 20, child)):
        content = Content.objects.create(
            library=library,
            storage_key=f"test/{uuid.uuid4()}.pdf",
            size_bytes=size,
            sha256="a" * 64,
        )
        files.append(
            Entry.objects.create(
                library=library, name=name, parent=parent, content=content, modified_by=admin
            )
        )
    library.usage_bytes = 60
    library.save(update_fields=("usage_bytes",))
    library.refresh_from_db()
    return library, root, child, files


def test_explorer_root_nested_search_and_paths(scope, explorer_tree):
    library, root, child, files = explorer_tree
    baseline = (AuditEvent.objects.count(), PublicationJob.objects.count(), library.source_revision)
    result = read_explorer(actor=scope[0], library_id=library.id)
    assert [item.entry.name for item in result.items] == ["Alpha", "Zulu", "A.pdf", "b.pdf"]
    assert result.breadcrumbs == () and result.current_folder is None
    assert (result.file_count, result.usage_bytes) == (3, 60)
    nested = read_explorer(actor=scope[0], library_id=library.id, folder_id=child.id)
    assert nested.breadcrumbs == (root, child)
    assert nested.items[0].entry == files[-1] and nested.items[0].ancestors == (root, child)
    search = read_explorer(actor=scope[0], library_id=library.id, query="  NEEDLE  ")
    assert [item.entry for item in search.items] == [files[-1]]
    assert search.items[0].ancestors == (root, child)
    folder_search = read_explorer(actor=scope[0], library_id=library.id, query="child")
    assert folder_search.items[0].ancestors == (root,)
    assert result.operations == {
        "preview",
        "download",
        "delete",
        "upload",
        "create_folder",
        "rename",
        "move",
        "replace",
    }
    library.refresh_from_db()
    assert (
        AuditEvent.objects.count(),
        PublicationJob.objects.count(),
        library.source_revision,
    ) == baseline


@pytest.mark.parametrize("sort", ["name", "modified", "size"])
@pytest.mark.parametrize("descending", [False, True])
def test_explorer_ordering_and_stable_pages(scope, explorer_tree, sort, descending):
    library, _, _, files = explorer_tree
    now = timezone.now()
    Entry.objects.filter(pk=files[0].id).update(modified_at=now - timedelta(days=1))
    Entry.objects.filter(pk=files[1].id).update(modified_at=now)
    result = read_explorer(
        actor=scope[0], library_id=library.id, sort=sort, descending=descending, page_size=2
    )
    assert all(item.entry.is_folder for item in result.items)
    assert result.page.paginator.count == 4 and result.page.paginator.num_pages == 2
    second = read_explorer(
        actor=scope[0], library_id=library.id, sort=sort, descending=descending, page_size=2, page=2
    )
    expected = [files[1], files[0]] if sort == "name" else [files[0], files[1]]
    if descending:
        expected.reverse()
    assert [item.entry for item in second.items] == expected
    Entry.objects.filter(pk__in=[f.id for f in files]).update(modified_at=now)
    tied = read_explorer(
        actor=scope[0], library_id=library.id, sort="modified", descending=descending
    )
    assert [item.entry.id for item in tied.items if not item.entry.is_folder] == sorted(
        [f.id for f in files[:2]]
    )


def test_explorer_bounded_database_queries_and_invalid_inputs(
    scope, explorer_tree, django_assert_num_queries
):
    library, root, child, files = explorer_tree
    Entry.objects.bulk_create(
        [
            Entry(library=library, name=f"Folder {i:03}", is_folder=True, modified_by=scope[0])
            for i in range(110)
        ]
    )
    # Comparing query counts makes sure a larger result set adds no per-result
    # queries; ancestry joins are bounded by the existing five-folder invariant.
    with CaptureQueriesContext(connection) as small_queries:
        read_explorer(actor=scope[0], library_id=library.id, page_size=1)
    with django_assert_num_queries(len(small_queries)) as large_queries:
        first = read_explorer(actor=scope[0], library_id=library.id, page_size=100)
    assert any(
        'FROM "file_library_entry"' in query["sql"] and "LIMIT 100" in query["sql"]
        for query in large_queries.captured_queries
    )
    assert len(first.items) == 100 and first.page.paginator.count == 114
    last = read_explorer(actor=scope[0], library_id=library.id, page=5)
    assert len(last.items) == 14
    with pytest.raises(EmptyPage):
        read_explorer(actor=scope[0], library_id=library.id, page=6)
    for size in (0, -1, 101, True):
        with pytest.raises(services.LibraryError):
            read_explorer(actor=scope[0], library_id=library.id, page_size=size)
    for folder_id in (uuid.uuid4(), files[0].id):
        with pytest.raises(services.LibraryError):
            read_explorer(actor=scope[0], library_id=library.id, folder_id=folder_id)
    with pytest.raises(services.LibraryError):
        read_explorer(actor=scope[0], library_id=library.id, sort=cast(Sort, "unknown"))


def test_explorer_authorization_temporary_access_and_stale_grants(scope, explorer_tree):
    admin, station_admin, other, department, outside, station, second = scope
    library, root, child, files = explorer_tree
    station_library = services.get_library(station_admin, station_id=station.id)
    assert (
        read_explorer(actor=station_admin, library_id=station_library.id).access_mode
        == services.ACCESS_NORMAL
    )
    other_library = services.get_library(other, department_id=outside.id)
    services.create_folder(actor=other, library_id=other_library.id, name="Needle outside")
    search = read_explorer(actor=admin, library_id=library.id, query="Needle")
    assert [item.entry for item in search.items] == [files[-1]]
    for actor, target in ((station_admin, library), (other, library), (admin, station_library)):
        with pytest.raises(PermissionDenied):
            read_explorer(actor=actor, library_id=target.id)
    grant = services.activate_temporary_access(actor=admin, station=station)
    temporary = read_explorer(actor=admin, library_id=station_library.id)
    assert temporary.access_mode == services.ACCESS_TEMPORARY
    assert temporary.operations == {"preview", "download", "delete"}
    with pytest.raises(services.LibraryError):
        read_explorer(actor=admin, library_id=station_library.id, folder_id=root.id)
    grant.expires_at = timezone.now() - timedelta(seconds=1)
    grant.activated_at = grant.expires_at - timedelta(hours=6)
    grant.save(update_fields=("expires_at", "activated_at"))
    with pytest.raises(PermissionDenied):
        read_explorer(actor=admin, library_id=station_library.id)
    grant.refresh_from_db()
    assert grant.revocation_reason == "expired"
    SystemRole.objects.create(user=other)
    with pytest.raises(PermissionDenied):
        read_explorer(actor=other, library_id=library.id)
    DepartmentMembership.objects.filter(user=admin).update(status="SUSPENDED")
    with pytest.raises(PermissionDenied):
        read_explorer(actor=admin, library_id=library.id)


def test_folder_ancestry_depth_deleted_folder_and_defensive_cycle_checks(scope):
    library = library_for(scope)
    folder = None
    chain = []
    for depth in range(5):
        folder = services.create_folder(
            actor=scope[0],
            library_id=library.id,
            name=str(depth),
            parent_id=folder.id if folder else None,
        )
        chain.append(folder)
    assert folder is not None
    result = read_explorer(actor=scope[0], library_id=library.id, folder_id=folder.id)
    assert result.breadcrumbs == tuple(chain)
    with pytest.raises(services.LibraryError):
        services.create_folder(
            actor=scope[0], library_id=library.id, name="Too deep", parent_id=folder.id
        )
    deleted_id = folder.id
    services.delete_entry(actor=scope[0], library_id=library.id, entry_id=folder.id)
    with pytest.raises(services.LibraryError):
        read_explorer(actor=scope[0], library_id=library.id, folder_id=deleted_id)
    chain[0].parent = chain[0]  # Invalid in-memory ancestry must not loop forever.
    with pytest.raises(services.LibraryError):
        services.folder_ancestry(library, chain[0])


def _reauthenticate(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


def test_department_publication_endpoint_coalesces_and_rejects_obsolete_form(
    scope, client, monkeypatch, django_capture_on_commit_callbacks
):
    admin, _, _, department, _, station, _ = scope
    url = reverse("publications-department-files-publish", args=(department.id,))
    wake = Mock()
    monkeypatch.setattr("apps.publications.wake.wake_publication_build_worker", wake)
    client.force_login(admin)
    assert client.get(url).status_code == 405
    assert client.post(url).url.startswith(reverse("accounts-reauthenticate"))
    assert not Library.objects.filter(department=department).exists()
    _reauthenticate(client)
    grant = services.activate_temporary_access(actor=admin, station=station)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url)
    assert response.status_code == 302 and response.url == reverse(
        "publications-list", args=(department.id,)
    )
    library = Library.objects.get(department=department)
    job = PublicationJob.objects.get(
        department=department, station=None, dataset_type_code="department_files"
    )
    assert job.trigger_type == "USER_REQUEST" and job.requested_by_id == admin.id
    assert job.not_before is not None and job.not_before <= timezone.now()
    grant.refresh_from_db()
    assert grant.revocation_reason == "department_mutation"
    first_job_id = job.id
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, HTTP_HX_REQUEST="true")
        legacy = client.post(reverse("file-library", args=(library.id,)), {"action": "publish"})
    assert response.status_code == 204 and response["HX-Redirect"] == reverse(
        "publications-list", args=(department.id,)
    )
    assert legacy.status_code == 403
    assert (
        PublicationJob.objects.filter(department=department, station=None).get().id == first_job_id
    )
    assert wake.call_count == 2
    assert (
        AuditEvent.objects.filter(
            action="file_library.publish_requested", target_uuid=library.id, department=department
        ).count()
        == 2
    )
    library.refresh_from_db()
    assert library.source_revision == 0 and library.usage_bytes == 0


def test_department_publication_endpoint_denies_other_roles_and_csrf(scope, client):
    admin, station_admin, other, department, outside, station, _ = scope
    url = reverse("publications-department-files-publish", args=(department.id,))
    assert client.post(url).status_code == 302
    for actor in (station_admin, other):
        client.force_login(actor)
        _reauthenticate(client)
        assert client.post(url).status_code == 403
    SystemRole.objects.create(user=other)
    assert client.post(url).status_code == 403
    client.force_login(admin)
    _reauthenticate(client)
    assert (
        client.post(
            reverse("publications-department-files-publish", args=(outside.id,))
        ).status_code
        == 403
    )
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(admin)
    assert csrf_client.post(url).status_code == 403
    assert not PublicationJob.objects.exists()
    DepartmentMembership.objects.filter(user=admin).update(status="SUSPENDED")
    assert client.post(url).status_code == 403


@pytest.mark.parametrize("expired_grant", [False, True])
def test_department_publication_endpoint_rolls_back_on_audit_failure(
    scope, client, monkeypatch, expired_grant
):
    admin, _, _, department, _, station, _ = scope
    grant = None
    if expired_grant:
        grant = services.activate_temporary_access(actor=admin, station=station)
        grant.expires_at = timezone.now() - timedelta(seconds=1)
        grant.activated_at = grant.expires_at - timedelta(hours=6)
        grant.save(update_fields=("expires_at", "activated_at"))
    client.force_login(admin)
    _reauthenticate(client)
    original = record_event

    def fail(**kwargs):
        if kwargs["action"] == "file_library.publish_requested":
            raise RuntimeError("Audit unavailable")
        return original(**kwargs)

    monkeypatch.setattr("apps.audit.services.record_event", fail)
    before = AuditEvent.objects.count()
    before_libraries = set(Library.objects.values_list("id", flat=True))
    before_jobs = set(PublicationJob.objects.values_list("id", flat=True))
    before_scopes = set(DatasetScopeState.objects.values_list("id", flat=True))
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        client.post(reverse("publications-department-files-publish", args=(department.id,)))
    assert set(Library.objects.values_list("id", flat=True)) == before_libraries
    assert set(PublicationJob.objects.values_list("id", flat=True)) == before_jobs
    assert set(DatasetScopeState.objects.values_list("id", flat=True)) == before_scopes
    assert AuditEvent.objects.count() == before + int(expired_grant)
    if grant is not None:
        grant.refresh_from_db()
        assert grant.revocation_reason == "expired"


@pytest.mark.django_db(transaction=True)
def test_decimal_migration_preserves_explicit_and_legacy_changes_independently(scope, capsys):
    admin, _, _, department, outside, station, _ = scope
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [("file_library", "0008_department_quota_policy")]
    try:
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        Policy = old_apps.get_model("file_library", "DepartmentQuotaPolicy")
        for dep in (department, outside):
            Policy.objects.create(department_id=dep.id)
        # Explicitly changed back to the old default protects this field only.
        record_event(
            action="file_library.quota_changed",
            actor_user=admin,
            department=department,
            target_type="department_quota_policy",
            target_uuid=department.id,
            metadata={
                "policy": "department_files_bytes",
                "old_quota_bytes": 99,
                "new_quota_bytes": 1_073_741_824,
            },
        )
        library = Library.objects.create(station=station, source_revision=17, usage_bytes=42)
        content = Content.objects.create(
            library=library, storage_key=f"test/{uuid.uuid4()}.pdf", size_bytes=42, sha256="a" * 64
        )
        Entry.objects.create(
            library=library, content=content, modified_by=admin, name="Retained.pdf"
        )
        # A legacy per-station setting affects the shared station policy.
        record_event(
            action="file_library.quota_changed",
            actor_user=admin,
            department=department,
            station=station,
            target_type="library",
            target_uuid=library.id,
            metadata={"old_quota_bytes": 22, "new_quota_bytes": 1_073_741_824},
        )
        custom = Department.objects.create(name="Custom", short_code="C", created_by=admin)
        Policy.objects.create(
            department_id=custom.id, department_files_bytes=123, station_files_bytes=456
        )
        independent = Department.objects.create(
            name="Independent", short_code="I", created_by=admin
        )
        Policy.objects.create(department_id=independent.id)
        record_event(
            action="file_library.quota_changed",
            actor_user=admin,
            department=independent,
            target_type="department_quota_policy",
            target_uuid=independent.id,
            metadata={
                "policy": "station_files_bytes",
                "old_quota_bytes": 99,
                "new_quota_bytes": 1_073_741_824,
            },
        )
        ambiguous = Department.objects.create(name="Ambiguous", short_code="AMB", created_by=admin)
        Policy.objects.create(department_id=ambiguous.id)
        record_event(
            action="file_library.quota_changed",
            actor_user=admin,
            department=ambiguous,
            target_type="unknown",
            metadata={},
        )
        department_only = Department.objects.create(
            name="Department only", short_code="DO", created_by=admin
        )
        Policy.objects.create(department_id=department_only.id)
        legacy_library = Library.objects.create(department=department_only)
        record_event(
            action="file_library.quota_changed",
            actor_user=admin,
            department=department_only,
            target_type="library",
            target_uuid=legacy_library.id,
            metadata={"old_quota_bytes": 99, "new_quota_bytes": 1_073_741_824},
        )
        before_events = list(AuditEvent.objects.values().order_by("id"))
        before_jobs = list(PublicationJob.objects.values())
        before_entries = list(Entry.objects.values().order_by("id"))
        before_content = list(Content.objects.values().order_by("id"))
        MigrationExecutor(connection).migrate(latest)

        def values(dep):
            return DepartmentQuotaPolicy.objects.values_list(
                "department_files_bytes", "station_files_bytes"
            ).get(department=dep)

        assert values(department) == (1_073_741_824, 1_073_741_824)
        assert values(outside) == (5_000_000_000, 2_000_000_000)
        assert values(custom) == (123, 456)
        assert values(independent) == (5_000_000_000, 1_073_741_824)
        assert values(ambiguous) == (1_073_741_824, 1_073_741_824)
        assert values(department_only) == (1_073_741_824, 2_000_000_000)
        library.refresh_from_db()
        assert (library.source_revision, library.usage_bytes) == (17, 42)
        assert list(AuditEvent.objects.values().order_by("id")) == before_events
        assert list(PublicationJob.objects.values()) == before_jobs
        assert list(Entry.objects.values().order_by("id")) == before_entries
        assert list(Content.objects.values().order_by("id")) == before_content
        output = capsys.readouterr().out
        assert "department_files_bytes: 2 updated, 4 preserved" in output
        assert "station_files_bytes: 2 updated, 4 preserved" in output
        # A data reversal never rewrites later administrator choices.
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        assert values(outside) == (5_000_000_000, 2_000_000_000)
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize(
    "ambiguous",
    ["missing_history", "unscoped_event", "custom_configuration", "custom_department_default"],
)
def test_decimal_backfill_ambiguous_provenance_preserved(
    scope, monkeypatch, settings, ambiguous, capsys
):
    module = importlib.import_module("apps.file_library.migrations.0009_decimal_quota_defaults")
    department = scope[3]
    policy = DepartmentQuotaPolicy.objects.create(
        department=department,
        department_files_bytes=1_073_741_824,
        station_files_bytes=1_073_741_824,
    )
    if ambiguous == "missing_history":
        recorder = Mock()
        recorder.migration_qs.filter.return_value = []
        monkeypatch.setattr(module, "MigrationRecorder", lambda connection: recorder)
    elif ambiguous == "unscoped_event":
        record_event(
            action="file_library.quota_changed", actor_user=scope[0], target_type="unknown"
        )
    elif ambiguous == "custom_configuration":
        settings.FILE_LIBRARY_QUOTA_BYTES = 987654321
    else:
        settings.FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES = 987654321
    from django.apps import apps

    with connection.schema_editor() as editor:
        module.backfill(apps, editor)
    policy.refresh_from_db()
    assert (policy.department_files_bytes, policy.station_files_bytes) == (
        1_073_741_824,
        2_000_000_000 if ambiguous == "custom_department_default" else 1_073_741_824,
    )
    assert "0 updated, 1 preserved" in capsys.readouterr().out


@pytest.mark.django_db(transaction=True)
def test_decimal_backfill_waits_for_quota_writer_before_reading_audit(scope):
    from django.apps import apps

    module = importlib.import_module("apps.file_library.migrations.0009_decimal_quota_defaults")
    admin, _, _, department, _, _, _ = scope
    DepartmentQuotaPolicy.objects.create(
        department=department,
        department_files_bytes=1_073_741_824,
        station_files_bytes=1_073_741_824,
    )
    writer_ready, release_writer, lock_started, audit_read = (Event() for _ in range(4))

    def writer():
        close_old_connections()
        try:
            with transaction.atomic():
                services.set_department_quotas(
                    actor=admin, department=department, department_files_bytes=17
                )
                services.set_department_quotas(
                    actor=admin, department=department, department_files_bytes=1_073_741_824
                )
                writer_ready.set()
                assert release_writer.wait(20), "Quota writer was not released."
        finally:
            close_old_connections()

    def migrating():
        close_old_connections()

        def observe(execute, sql, params, many, context):
            if (
                sql.startswith("SELECT")
                and '"file_library_departmentquotapolicy"' in sql
                and "FOR UPDATE" in sql
            ):
                lock_started.set()
            if sql.startswith("SELECT") and '"audit_event"' in sql:
                audit_read.set()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(observe), connection.schema_editor() as editor:
                module.backfill(apps, editor)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writing = executor.submit(writer)
        try:
            assert writer_ready.wait(20), "Quota writer did not reach its transaction barrier."
            migration = executor.submit(migrating)
            assert lock_started.wait(20), "Backfill did not attempt to lock policies."
            assert not audit_read.is_set(), "Backfill read stale audit evidence before waiting."
        finally:
            release_writer.set()
        writing.result(timeout=20)
        migration.result(timeout=20)
    policy = DepartmentQuotaPolicy.objects.get(department=department)
    assert policy.department_files_bytes == 1_073_741_824
    assert policy.station_files_bytes == 2_000_000_000
    assert audit_read.is_set()
    assert AuditEvent.objects.filter(action="file_library.quota_changed").count() == 2


def test_native_deployment_check_fails_closed_on_windows(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from apps.file_library.management.commands import verify_library_pdf_sanitizer

    monkeypatch.setattr(verify_library_pdf_sanitizer.sys, "platform", "win32")
    with pytest.raises(CommandError, match="deployed Linux"):
        call_command("verify_library_pdf_sanitizer")
