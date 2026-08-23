"""Regression tests for the tablet list templates (polling cadence and dropdown layout)."""

import uuid
from datetime import datetime
from types import SimpleNamespace

from django.template.loader import render_to_string


def _department():
    return SimpleNamespace(id=uuid.uuid4())


def _counts(total=1, operational=1, active=0, inactive=1, lost=0, retired=0):
    return SimpleNamespace(
        total=total,
        operational=operational,
        active=active,
        inactive=inactive,
        lost=lost,
        retired=retired,
    )


def _render_status_summary():
    return render_to_string(
        "tablets/_tablet_status_summary.html",
        {"department": _department(), "counts": _counts(), "last_updated": datetime.now()},
    )


def _tablet(status="ACTIVE"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Command iPad",
        asset_number="TAB-0042",
        status=status,
        installation_status=None,
        current_assignment=[],
        current_installations=[],
        last_seen=None,
        active=True,
        has_open_vehicle=False,
    )


def _page():
    return SimpleNamespace(
        start_index=1,
        end_index=1,
        has_other_pages=False,
        number=1,
        paginator=SimpleNamespace(num_pages=1),
    )


def _render_results(*tablets, total_count=None):
    return render_to_string(
        "tablets/_tablet_results.html",
        {
            "department": _department(),
            "tablets": list(tablets),
            "total_count": total_count if total_count is not None else len(tablets),
            "matched_count": len(tablets),
            "page": _page(),
            "page_query": "",
            "list_url": "/departments/test/tablets/",
            "results_base": "/departments/test/tablets/",
        },
    )


def _render_list():
    return render_to_string(
        "tablets/list.html",
        {
            "department": _department(),
            "counts": _counts(),
            "last_updated": datetime.now(),
            "filters": {
                "search": "",
                "status": "",
                "installation": "",
                "station": "",
                "vehicle": "",
            },
            "statuses": (
                ("INACTIVE", "Inactive"),
                ("ACTIVE", "Active"),
                ("LOST", "Lost"),
                ("RETIRED", "Retired"),
            ),
            "installation_options": (
                ("current", "Current"),
                ("stale", "Stale"),
                ("none", "No installation"),
            ),
            "station_options": (),
            "vehicle_options": (),
            "sort": "",
            "dir": "",
            "tablets": [_tablet()],
            "page": _page(),
            "page_query": "",
            "total_count": 1,
            "matched_count": 1,
            "list_url": "/departments/test/tablets/",
            "results_base": "/departments/test/tablets/",
        },
    )


def _render_adoption_status(state):
    return render_to_string(
        "tablets/_adoption_status.html",
        {
            "state": state,
            "mode": "adoption",
            "status_url": "/departments/test/tablets/status/",
            "invitation": SimpleNamespace(expires_at=datetime.now()),
            "department": _department(),
            "tablet": _tablet(),
        },
    )


# --- status polling ----------------------------------------------------------


def test_status_summary_polls_every_30s_without_load_trigger():
    html = _render_status_summary()
    assert 'hx-trigger="every 30s"' in html
    assert "load," not in html
    assert 'hx-trigger="load' not in html


def test_status_summary_has_single_polling_root():
    html = _render_status_summary()
    assert html.count('id="tablet-status-summary"') == 1
    assert html.count("hx-get=") == 1
    assert html.count("hx-trigger=") == 1


def test_status_summary_has_no_sub_second_polling():
    html = _render_status_summary()
    assert "every 1s" not in html
    assert "every 2s" not in html


# --- results table layout ----------------------------------------------------


def test_results_table_participates_in_page_flow_without_scroll_wrapper():
    html = _render_results(_tablet())
    assert "table-responsive" not in html
    assert "overflow-auto" not in html
    assert "overflow-x-auto" not in html


def test_results_table_keeps_identity_state_and_actions():
    html = _render_results(_tablet())
    assert '<th scope="col">Tablet</th>' in html
    assert '<th scope="col">Asset state</th>' in html
    assert '<th scope="col">Installation</th>' in html
    assert "Actions" in html
    assert "View details" not in html
    assert "dropdown-toggle" in html
    assert "Command iPad" in html


def test_results_table_hides_secondary_columns_responsively():
    html = _render_results(_tablet())
    assert 'class="d-none d-md-table-cell">Assignment</th>' in html
    assert 'class="d-none d-md-table-cell">Last contact</th>' in html


def test_list_uses_live_server_side_filters_with_a_one_second_search_debounce():
    html = _render_list()

    assert 'id="tablet-filter-form"' in html
    assert 'name="search"' in html
    assert 'placeholder="Search by name or asset number"' in html
    assert 'hx-trigger="input changed delay:1s"' in html
    assert 'name="status"' in html
    assert 'name="station"' in html
    assert 'name="vehicle"' in html
    assert 'hx-target="#tablet-results"' in html
    assert 'hx-include="#tablet-filter-form"' in html
    assert ">Reset</a>" in html


def test_list_keeps_asset_and_installation_lifecycle_filters_distinct():
    html = _render_list()

    assert 'for="tablet-status">Asset state</label>' in html
    assert 'for="tablet-installation">Installation</label>' in html
    assert "Active and inactive (default)" in html
    assert "PENDING" not in html
    assert "REVOKED" not in html
    assert "REPLACED" not in html


# --- adoption / reactivation status polling ----------------------------------


def test_adoption_status_polls_every_4s_without_load_trigger():
    html = _render_adoption_status("waiting")
    assert 'hx-trigger="every 4s"' in html
    assert 'hx-trigger="load' not in html
    assert "load," not in html


def test_adoption_status_waiting_state_is_self_replacing():
    html = _render_adoption_status("waiting")
    assert 'id="adoption-status"' in html
    assert 'hx-swap="outerHTML"' in html


def test_adoption_status_terminal_states_stop_polling():
    for state in ("completed", "expired", "revoked"):
        html = _render_adoption_status(state)
        assert "hx-get=" not in html
        assert "hx-trigger=" not in html
        assert "hx-swap=" not in html
