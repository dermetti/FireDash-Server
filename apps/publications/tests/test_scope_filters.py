# ruff: noqa: F811
"""Department publication filtering is bounded, tenant-scoped and bookmarkable."""

import re
import uuid
from html import unescape

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.authorization.models import StationAdminAssignment
from apps.organizations.models import Station
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.tests.test_publications_ui import (
    _current_scope,
    publication_ui_context,  # noqa: F401
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def scopes(publication_ui_context):
    admin, outsider, department, other_department, station = publication_ui_context
    current, _ = _current_scope(department=department)
    station_scope = DatasetScopeState.objects.create(
        department=department,
        station=station,
        dataset_type_code="station_files",
        scope_type="STATION",
    )
    other_station = Station.objects.create(department=department, name="North", short_code="N")
    scheduled = DatasetScopeState.objects.create(
        department=department,
        station=other_station,
        dataset_type_code="station_files",
        scope_type="STATION",
    )
    PublicationJob.objects.create(
        department=department,
        station=other_station,
        dataset_type_code="station_files",
        scope_state=scheduled,
        scope_type="STATION",
        status="PENDING",
        source_revision=1,
        trigger_type="USER_REQUEST",
    )
    foreign_station = Station.objects.create(
        department=other_department, name="Foreign", short_code="F"
    )
    foreign = DatasetScopeState.objects.create(
        department=other_department,
        station=foreign_station,
        dataset_type_code="station_files",
        scope_type="STATION",
    )
    return current, station_scope, scheduled, foreign


@pytest.mark.parametrize(
    "mode,state,indices",
    [
        ("department", "", [0]),
        ("all", "", [0, 1, 2]),
        ("station", "", [1]),
        ("department", "current", [0]),
        ("all", "scheduled", [2]),
        ("station", "not_published", [1]),
        ("station", "scheduled", []),
        ("department", "scheduled", []),
        ("all", "building", []),
        ("all", "failed", []),
        ("station", "current", []),
    ],
)
def test_scope_then_status_filters(client, publication_ui_context, scopes, mode, state, indices):
    admin, _, department, _, station = publication_ui_context
    client.force_login(admin)
    params = {"scope": mode, "state": state, "station_id": str(station.id)}
    response = client.get(reverse("publications-list", args=(department.id,)), params)
    assert response.status_code == 200
    assert {row["scope_id"] for row in response.context["scope_rows"]} == {
        scopes[i].id for i in indices
    }
    assert response.context["total_count"] == len(indices)
    assert str(scopes[3].id).encode() not in response.content
    if mode == "all" and not state:
        assert station.name.encode() in response.content
        assert station.short_code.encode() in response.content
    if mode != "station":
        assert "station_id" not in response.context["page_query"]
    else:
        assert station.name.encode() in response.content
        assert station.short_code.encode() in response.content


def test_default_reset_and_accessible_filter_panel(client, publication_ui_context, scopes):
    admin, _, department, _, _ = publication_ui_context
    client.force_login(admin)
    url = reverse("publications-list", args=(department.id,))
    # Each fresh visit defaults to department, irrespective of previous visits.
    client.get(url, {"scope": "all"})
    response = client.get(url)
    content = response.content.decode()
    assert response.context["scope_mode"] == "department"
    assert response.context["total_count"] == 1
    radios = re.findall(r'<input type="radio"[^>]+>', content)
    assert len(radios) == 3
    assert sum(" checked" in radio for radio in radios) == 1
    assert 'id="scope-department" value="department" checked' in content
    assert 'role="group" aria-label="Publication scope"' in content
    assert 'class="publication-scope-switch d-flex flex-wrap gap-2"' in content
    assert content.count('class="scope-selected" aria-hidden="true">&#10003;</span>') == 3
    assert ".btn-check:checked + label .scope-selected" in content
    assert (
        '<label for="publication-state-filter" class="form-label">Publication status</label>'
        in content
    )
    assert 'hx-target="#publication-browser" hx-swap="outerHTML" hx-push-url="true"' in content
    assert content.count('name="state"') == 1
    assert 'name="page"' not in content  # Applying any changed criteria resets pagination.
    assert 'hx-trigger="change"' not in content  # No premature station submission.
    assert "Department-wide bulk action" in content
    assert "regardless of the filters below" in content
    assert 'href="' + url + '">Reset</a>' in content


@pytest.mark.parametrize("state", ["building", "failed"])
def test_station_running_and_failed_states_match_list_rows(
    client, publication_ui_context, scopes, state
):
    admin, _, department, _, _ = publication_ui_context
    scope = scopes[2]
    job = PublicationJob.objects.get(scope_state=scope)
    job.status = "RUNNING" if state == "building" else "FAILED"
    job.save(update_fields=("status",))
    DatasetPublication.objects.create(
        department=department,
        station=scope.station,
        scope_type="STATION",
        scope_state=scope,
        dataset_type_code="station_files",
        version_number=1,
        schema_version=1,
        source_revision=1,
        status="BUILDING" if state == "building" else "FAILED",
    )
    client.force_login(admin)
    for mode in ("station", "all"):
        response = client.get(
            reverse("publications-list", args=(department.id,)),
            {
                "scope": mode,
                "station_id": scope.station_id,
                "state": state,
            },
        )
        assert response.context["total_count"] == 1
        assert response.context["scope_rows"][0]["scope_id"] == scope.id
        assert response.context["scope_rows"][0]["state"] == state.upper()


def test_station_pagination_retains_uuid_and_status(
    client, publication_ui_context, scopes, monkeypatch
):
    admin, _, department, _, station = publication_ui_context
    DatasetScopeState.objects.create(
        department=department,
        station=station,
        scope_type="STATION",
        dataset_type_code="station_personnel",
    )
    monkeypatch.setattr("apps.publications.views.SCOPE_PAGE_SIZE", 1)
    client.force_login(admin)
    url = reverse("publications-list", args=(department.id,))
    params = {"scope": "station", "station_id": station.id, "state": "not_published"}
    response = client.get(url, params, HTTP_HX_REQUEST="true")
    assert response.context["total_count"] == 2
    assert (
        response.context["page_query"]
        == f"scope=station&station_id={station.id}&state=not_published"
    )
    next_page = client.get(url, params | {"page": 2}, HTTP_HX_REQUEST="true")
    assert next_page.context["page"].number == 2
    assert b'id="scope-station" value="station" checked' in next_page.content
    assert b'value="not_published" selected' in next_page.content
    assert station.name.encode() in next_page.content


@pytest.mark.parametrize("invalid", ["malformed", "foreign", "inactive", "missing", ""])
def test_invalid_station_never_falls_back_to_all(client, publication_ui_context, scopes, invalid):
    admin, _, department, _, station = publication_ui_context
    client.force_login(admin)
    if invalid == "foreign":
        value = str(scopes[3].station_id)
    elif invalid == "inactive":
        station.active = False
        station.save(update_fields=("active",))
        value = str(station.id)
    elif invalid == "missing":
        value = str(uuid.uuid4())
    else:
        value = invalid
    response = client.get(
        reverse("publications-list", args=(department.id,)),
        {
            "scope": "station",
            "station_id": value,
        },
    )
    assert response.context["total_count"] == 0
    assert not response.context["selected_station"]
    assert b"Select a station to view its publications." in response.content
    assert b"Review capacity and build" not in response.content
    if value:
        assert b"The selected station is unavailable." in response.content
    assert b"Foreign" not in response.content


def test_empty_states_preserve_scope_when_clearing_status(client, publication_ui_context, scopes):
    admin, _, department, _, station = publication_ui_context
    client.force_login(admin)
    url = reverse("publications-list", args=(department.id,))
    response = client.get(url, {"scope": "station", "station_id": station.id, "state": "failed"})
    assert b"No publications match these filters." in response.content
    match = re.search(r'href="([^"]+)">Clear status filter', response.content.decode())
    assert match is not None
    link = unescape(match[1])
    assert link == f"{url}?scope=station&station_id={station.id}"
    assert client.get(link).context["total_count"] == 1
    empty = Station.objects.create(department=department, name="Empty", short_code="E")
    assert (
        b"No publication scopes are available for this station."
        in client.get(
            url,
            {
                "scope": "station",
                "station_id": empty.id,
            },
        ).content
    )


def test_pagination_htmx_status_and_history_restore(
    client, publication_ui_context, scopes, monkeypatch
):
    admin, _, department, _, _ = publication_ui_context
    client.force_login(admin)
    monkeypatch.setattr("apps.publications.views.SCOPE_PAGE_SIZE", 1)
    url = reverse("publications-list", args=(department.id,))
    for endpoint in (url, reverse("publications-status", args=(department.id,))):
        response = client.get(endpoint, {"scope": "all", "page": 2}, HTTP_HX_REQUEST="true")
        content = response.content.decode()
        assert response.context["page"].number == 2
        assert response.context["total_count"] == 3
        assert 'id="publication-browser"' in content
        assert 'id="scope-all" value="all" checked' in content
        assert "?page=3&amp;scope=all" in content
        assert f'hx-get="{url}?page=3&amp;scope=all"' in content
        assert 'hx-push-url="true"' in content
        assert "North" in content and "&middot; N" in content
    restored = client.get(
        url,
        {"scope": "station", "station_id": scopes[1].station_id},
        HTTP_HX_REQUEST="true",
        HTTP_HX_HISTORY_RESTORE_REQUEST="true",
    )
    assert b"<!DOCTYPE html>" in restored.content or b"<!doctype html>" in restored.content
    assert b'id="scope-station" value="station" checked' in restored.content


def test_station_search_bounded_fallback_and_authority(client, publication_ui_context, scopes):
    admin, outsider, department, other_department, station = publication_ui_context
    Station.objects.bulk_create(
        [
            Station(department=department, name=f"Search station {i:03d}", short_code=f"S{i:03d}")
            for i in range(120)
        ]
    )
    Station.objects.create(
        department=department, name="Search inactive", short_code="OFF", active=False
    )
    client.force_login(admin)
    endpoint = reverse("publications-station-search", args=(department.id,))
    response = client.get(endpoint, {"q": "Search"}, HTTP_HX_REQUEST="true")
    assert len(response.context["results"]) == 20
    assert response.content.count(b'role="option"') == 20
    assert b"inactive" not in response.content and b"Foreign" not in response.content
    assert b"Search station 119" in client.get(endpoint, {"q": "S119"}).content
    url = reverse("publications-list", args=(department.id,))
    fallback = client.get(url, {"scope": "station", "station_q": "S119"})
    assert len(fallback.context["station_options"]) == 1
    content = fallback.content.decode()
    assert 'name="station_id" class="form-select"' in content
    assert "data-station-selector data-publication-station" in content
    assert 'aria-controls="station-listbox"' in content
    assert "Search station 119 (S119)" in content
    assert "Activate access" not in content and 'name="confirm"' not in content
    selected = fallback.context["station_options"][0]
    assert (
        client.get(url, {"scope": "station", "station_id": selected.id}).context["selected_station"]
        == selected
    )
    assert (
        client.get(reverse("publications-station-search", args=(other_department.id,))).status_code
        == 403
    )
    StationAdminAssignment.objects.create(user=outsider, station=station, created_by=admin)
    client.force_login(outsider)
    assert client.get(endpoint).status_code == 403
    assert client.get(url, {"scope": "all"}).status_code == 403


def test_many_scopes_are_filtered_and_paginated_before_read_model(
    client, publication_ui_context, scopes, monkeypatch
):
    admin, _, department, _, _ = publication_ui_context
    stations = Station.objects.bulk_create(
        [
            Station(department=department, name=f"Large {i:03d}", short_code=f"L{i:03d}")
            for i in range(125)
        ]
    )
    DatasetScopeState.objects.bulk_create(
        [
            DatasetScopeState(
                department=department,
                station=station,
                dataset_type_code="station_files",
                scope_type="STATION",
            )
            for station in stations
        ]
    )
    from apps.publications import views

    original = views.scope_operational_states_for_scopes
    batches = []

    def bounded(scopes):
        batches.append(len(scopes))
        assert len(scopes) <= 50
        return original(scopes)

    monkeypatch.setattr(views, "scope_operational_states_for_scopes", bounded)
    client.force_login(admin)
    url = reverse("publications-list", args=(department.id,))
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"scope": "all", "page": 2})
    assert response.context["total_count"] == 128
    assert len(response.context["scope_rows"]) == 50
    assert batches == [50, 0]
    # A constant query budget guards against per-row station/history lookups.
    assert len(queries) < 25
    assert not response.context["station_options"]
    batches.clear()
    response = client.get(url, {"scope": "station", "station_id": stations[-1].id})
    assert response.context["total_count"] == 1
    assert batches == [1, 0]
