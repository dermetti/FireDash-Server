"""Isolated query/payload measurements for publication presentation paths."""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.organizations.models import Station
from apps.publications.models import DatasetPublication, DatasetScopeState
from apps.publications.restore import scopes_with_eligible_predecessor
from apps.publications.state import scope_operational_states_for_scopes
from apps.publications.tests import test_publications_ui as ui_tests
from apps.publications.views import _decorate_scope_rows, _scope_history_context


@pytest.fixture
def publication_ui_context(db):
    return ui_tests.publication_ui_context.__wrapped__(db)


def _publication(*, department, scope, version_number, status):
    identity = uuid.uuid4()
    from apps.publications.paths import publication_artifact_relative_path

    path = (
        f"system/{identity}/artifact.bin"
        if scope.scope_type == "SYSTEM"
        else publication_artifact_relative_path(
            department_id=department.id, publication_id=identity
        )
    )
    return DatasetPublication.objects.create(
        id=identity,
        department=department,
        station=scope.station,
        scope_type=scope.scope_type,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
        schema_version=1,
        source_revision=1,
        status=status,
        artifact_ready=True,
        artifact_status="READY",
        artifact_path=path,
        artifact_size=1,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"w" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="test",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="test",
    )


@contextmanager
def measured_publications():
    original = DatasetPublication.from_db
    stats = {"objects": 0, "snapshot_bytes": 0, "snapshots": 0}

    def from_db(db, fields, values):
        obj = original(db, fields, values)
        stats["objects"] += 1
        if "source_snapshot" in fields:
            stats["snapshots"] += 1
            stats["snapshot_bytes"] += len(json.dumps(obj.source_snapshot))
        return obj

    with patch.object(DatasetPublication, "from_db", side_effect=from_db):
        with CaptureQueriesContext(connection) as queries:
            yield stats, queries
    stats["queries"] = len(queries)


@pytest.mark.django_db
@pytest.mark.parametrize("scope_count", [1, 10, 50])
def test_browser_query_scaling(publication_ui_context, scope_count, client):
    admin, _, department, _, _ = publication_ui_context
    scopes = []
    for i in range(scope_count):
        station = Station.objects.create(
            department=department, name=f"Station {i}", short_code=f"S{i}"
        )
        scope = DatasetScopeState.objects.create(
            department=department,
            station=station,
            scope_type="STATION",
            dataset_type_code="station_personnel",
        )
        current = _publication(
            department=department, scope=scope, version_number=3, status="PUBLISHED"
        )
        _publication(department=department, scope=scope, version_number=2, status="SUPERSEDED")
        scope.current_published_publication = current
        scope.save(update_fields=("current_published_publication",))
        scopes.append(scope)
    with measured_publications() as (stats, queries):
        rows = _decorate_scope_rows(scope_operational_states_for_scopes(scopes))
    assert all(row["has_eligible_predecessor"] for row in rows)
    assert stats["queries"] == 7
    assert stats["snapshots"] == 0
    assert stats["objects"] == 2 * scope_count
    assert not any("documentartifact" in q["sql"] for q in queries)
    print(f"browser scopes={scope_count}: {stats}")
    client.force_login(admin)
    with measured_publications() as (stats, queries):
        response = client.get(
            reverse("publications-list", args=(department.id,)), {"scope": "station"}
        )
    assert response.status_code == 200
    assert len(response.context["scope_rows"]) == scope_count
    assert stats["snapshots"] == 0
    assert stats["queries"] == 26
    print(f"browser request scopes={scope_count}: {stats}")


@pytest.mark.django_db
@pytest.mark.parametrize("history_count", [2, 25, 200])
@pytest.mark.parametrize("page_number", [1, 2])
def test_history_payload_scaling(publication_ui_context, history_count, page_number, client):
    admin, _, department, _, _ = publication_ui_context
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    payload = {"features": ["x" * 131072]}
    for version in range(1, history_count + 1):
        publication = _publication(
            department=department, scope=scope, version_number=version, status="SUPERSEDED"
        )
        DatasetPublication.objects.filter(pk=publication.pk).update(source_snapshot=payload)
    current = _publication(
        department=department, scope=scope, version_number=history_count + 1, status="PUBLISHED"
    )
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    with measured_publications() as (stats, queries):
        context = _scope_history_context(scope, page_number)
    page = context["history_page"]
    assert len(page.object_list) <= 25
    assert page.paginator.count == history_count + 1
    assert stats["queries"] == 5
    assert stats["snapshots"] == 0
    assert stats["objects"] == len(page.object_list)
    assert all(p.can_rollback for p in page.object_list if p.status == "SUPERSEDED")
    assert not any("documentartifact" in q["sql"] for q in queries)
    assert not any('"build_summary"' in q["sql"] or '"change_summary"' in q["sql"] for q in queries)
    expected_top = history_count + 1 - (page.number - 1) * 25
    assert page.object_list[0].version_number == expected_top
    print(f"history={history_count} page={page_number}: {stats}")
    client.force_login(admin)
    with measured_publications() as (stats, queries):
        response = client.get(
            reverse("publications-scope-detail", args=(scope.id,)), {"page": page_number}
        )
    assert response.status_code == 200
    assert stats["snapshots"] == 0
    assert response.context["history_page"].number == page.number
    assert stats["queries"] == 25
    print(f"history request={history_count} page={page_number}: {stats}")


def _empty_document(scope, version, *, status="SUPERSEDED", malformed=False):
    from apps.publications.models import DocumentGenerationKey, DocumentGenerationManifest

    publication = DatasetPublication.objects.create(
        scope_state=scope,
        department_id=scope.department_id,
        dataset_type_code=scope.dataset_type_code,
        version_number=version,
        schema_version=2,
        source_revision=1,
        status=status,
        artifact_ready=True,
        artifact_status="READY",
        source_snapshot={"fire_plans": []},
    )
    DocumentGenerationKey.objects.create(
        publication=publication,
        wrapped_key=b"k" * 40,
        wrapping_algorithm="AES-KW-RFC3394",
        kek_version="test",
    )
    DocumentGenerationManifest.objects.create(
        publication=publication,
        signature=b"s" * 64,
        signature_algorithm="Ed25519",
        signing_key_version="test",
        payload={
            "format": "document-generation-v2",
            "dataset_type": scope.dataset_type_code,
            "publication_id": str(publication.id),
            "version": version,
            "schema_version": 2,
            "documents": "invalid" if malformed else [],
        },
    )
    return publication


@pytest.mark.django_db
@pytest.mark.parametrize("history_count", [2, 25, 200])
@pytest.mark.parametrize("page_number", [1, 2])
def test_document_history_loads_only_relevant_page_candidates(
    publication_ui_context, history_count, page_number
):
    _, _, department, _, _ = publication_ui_context
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    old = _empty_document(scope, 1)
    for version in range(2, history_count + 1):
        DatasetPublication.objects.create(
            scope_state=scope,
            department=department,
            dataset_type_code=scope.dataset_type_code,
            version_number=version,
            schema_version=1 if version % 3 == 0 else 2,
            source_revision=1,
            status="OBSOLETE" if version % 3 == 1 else "SUPERSEDED",
            artifact_ready=True,
            artifact_status="READY",
            source_snapshot={"unused": "x" * 131072},
        )
    current = _empty_document(scope, history_count + 1)
    current.status = "PUBLISHED"
    current.save(update_fields=("status",))
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    with measured_publications() as (stats, queries):
        context = _scope_history_context(scope, page_number)
    page = context["history_page"]
    assert page.paginator.count == history_count + 1
    for publication in page.object_list:
        assert publication.can_rollback == (publication.id == old.id)
    assert stats["queries"] <= 9
    assert stats["snapshots"] <= 3  # predecessor + at most two relevant visible versions
    assert stats["objects"] <= len(page.object_list) + 3
    print(f"document history={history_count} page={page_number}: {stats}")


@pytest.mark.django_db
def test_document_predecessor_batches_skip_invalid_candidates(publication_ui_context):
    _, _, department, _, _ = publication_ui_context
    scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    _empty_document(scope, 1)
    for version in range(2, 20):
        _empty_document(scope, version, malformed=True)
    current = _empty_document(scope, 20)
    current.status = "PUBLISHED"
    current.save(update_fields=("status",))
    scope.current_published_publication = current
    scope.save(update_fields=("current_published_publication",))
    with measured_publications() as (stats, queries):
        assert scopes_with_eligible_predecessor([scope.id]) == {scope.id}
    assert stats["objects"] == 19
    assert stats["queries"] == 8
    print(f"document batches: {stats}")


@pytest.mark.django_db
def test_mixed_ownership_predecessor_checks_are_batched(publication_ui_context):
    _, _, department, _, station = publication_ui_context
    scopes = []
    for code, owner, selected_station in (
        ("dangerous_goods", None, None),
        ("department_hydrants", department, None),
        ("station_personnel", department, station),
    ):
        scope, _ = DatasetScopeState.objects.get_or_create(
            dataset_type_code=code,
            department=owner,
            station=selected_station,
            scope_type="SYSTEM"
            if owner is None
            else "STATION"
            if selected_station
            else "DEPARTMENT",
        )
        _publication(department=owner, scope=scope, version_number=1, status="SUPERSEDED")
        current = _publication(department=owner, scope=scope, version_number=2, status="PUBLISHED")
        scope.current_published_publication = current
        scope.save(update_fields=("current_published_publication",))
        scopes.append(scope)
    with measured_publications() as (stats, queries):
        assert scopes_with_eligible_predecessor([s.id for s in scopes]) == {s.id for s in scopes}
    assert stats["objects"] == stats["snapshots"] == 0
    assert stats["queries"] == 2
