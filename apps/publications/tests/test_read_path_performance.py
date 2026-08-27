"""PostgreSQL read-path regressions for publication scope presentation."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetScopeState
from apps.publications.state import dataset_publication_summaries
from apps.publications.views import _publication_list_context


def _department_with_station_scopes(*, total: int):
    admin = User.objects.create_user(f"read-path-{total}@example.test", "Read Path", "password")
    department = Department.objects.create(
        name=f"Read path {total}", short_code=f"RP{total}", created_by=admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    stations = Station.objects.bulk_create(
        [
            Station(
                department=department,
                name=f"Station {number:03d}",
                short_code=f"S{number:03d}",
            )
            for number in range(total)
        ]
    )
    DatasetScopeState.objects.bulk_create(
        [
            DatasetScopeState(
                department=department,
                station=station,
                dataset_type_code="station_personnel",
            )
            for station in stations
        ]
    )
    return department


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("total", (4, 50, 400))
def test_publication_list_query_count_is_bounded_and_only_enriches_visible_page(monkeypatch, total):
    """The list counts/pages scopes before its bounded publication batch reads."""
    assert connection.vendor == "postgresql"
    department = _department_with_station_scopes(total=total)
    request = RequestFactory().get("/publications/", {"page": "1"})
    calls: list[int] = []

    from apps.publications import views

    original = views.scope_operational_states_for_scopes

    def track_visible_scopes(scopes, *, now=None):
        calls.append(len(scopes))
        return original(scopes, now=now)

    def source_rebuild_called(*args, **kwargs):
        raise AssertionError("Normal publication list rendering must not rebuild canonical source.")

    monkeypatch.setattr(views, "scope_operational_states_for_scopes", track_visible_scopes)
    monkeypatch.setattr("apps.publications.builders.build_source_payload", source_rebuild_called)
    monkeypatch.setattr("apps.publications.builders.source_fingerprint", source_rebuild_called)

    with CaptureQueriesContext(connection) as queries:
        context = _publication_list_context(request, department)

    assert context["total_count"] == total
    assert len(context["scope_rows"]) == min(total, 50)
    assert calls == [min(total, 50)]
    # count + paged scopes + latest publications + active jobs + predecessor
    # lookup; the total number must not grow with the department's scope count.
    assert len(queries) == 5
    sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
    assert "source_snapshot" not in sql


@pytest.mark.django_db(transaction=True)
def test_data_hub_summary_is_batched_without_detailed_scope_rows_or_source_rebuild(monkeypatch):
    """A 400-scope module receives one aggregate state summary, not 400 rows."""
    assert connection.vendor == "postgresql"
    department = _department_with_station_scopes(total=400)

    def source_rebuild_called(*args, **kwargs):
        raise AssertionError("Data Hub must not rebuild canonical source on a normal GET.")

    def detailed_rows_called(*args, **kwargs):
        raise AssertionError("Data Hub must not materialize detailed scope row state.")

    monkeypatch.setattr("apps.publications.builders.build_source_payload", source_rebuild_called)
    monkeypatch.setattr("apps.publications.builders.source_fingerprint", source_rebuild_called)
    monkeypatch.setattr(
        "apps.publications.state.scope_operational_states_for_scopes", detailed_rows_called
    )

    with CaptureQueriesContext(connection) as queries:
        summaries = dataset_publication_summaries(
            department, dataset_type_codes={"station_personnel"}
        )

    summary = summaries["station_personnel"]
    assert summary["scope_count"] == 400
    assert summary["published_scope_count"] == 0
    # scopes + latest publications + active jobs. Empty linked-publication IDs
    # intentionally issue no extra SQL query.
    assert len(queries) == 3
    sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
    assert "source_snapshot" not in sql
