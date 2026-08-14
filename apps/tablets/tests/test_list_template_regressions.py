"""Regression tests for the tablet list templates (polling cadence and dropdown layout)."""

import uuid
from datetime import datetime
from types import SimpleNamespace

from django.template.loader import render_to_string


def _department():
    return SimpleNamespace(id=uuid.uuid4())


def _counts(total=1, active=0, pending=1, stale=0):
    return SimpleNamespace(total=total, active=active, pending=pending, stale=stale)


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
        current_assignment=[],
        last_seen=None,
        active=True,
        has_open_vehicle=False,
    )


def _render_results(*tablets, total_count=None):
    return render_to_string(
        "tablets/_tablet_results.html",
        {
            "department": _department(),
            "tablets": list(tablets),
            "total_count": total_count if total_count is not None else len(tablets),
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


def test_results_table_has_no_overflow_wrapper():
    html = _render_results(_tablet())
    assert "table-responsive" not in html


def test_results_table_keeps_identity_status_and_actions():
    html = _render_results(_tablet())
    assert '<th scope="col">Tablet</th>' in html
    assert '<th scope="col">Status</th>' in html
    assert "Actions" in html


def test_results_table_hides_secondary_columns_responsively():
    html = _render_results(_tablet())
    assert 'class="d-none d-md-table-cell">Vehicle</th>' in html
    assert 'class="d-none d-lg-table-cell">Station</th>' in html
    assert 'class="d-none d-md-table-cell">Last seen</th>' in html


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
