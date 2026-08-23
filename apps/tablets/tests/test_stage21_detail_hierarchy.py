"""Stage 2.1 Tablet detail hierarchy regression tests."""

from django.urls import reverse

from .conftest import _adopt  # noqa: E402


def test_detail_separates_asset_and_installation_sections(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    html = client.get(
        reverse("tablet-detail", args=(tablet.department_id, tablet.id))
    ).content.decode()

    assert "Tablet actions" in html
    assert "Current installation" in html
    assert "Installation history" in html


def test_reprovision_appears_under_installation_actions(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    html = client.get(
        reverse("tablet-detail", args=(tablet.department_id, tablet.id))
    ).content.decode()

    assert "Re-provision FireDash" in html
    assert "Revoke installation" in html
    assert "Transfer" in html


def test_no_forbidden_user_facing_wording(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    html = client.get(
        reverse("tablet-detail", args=(tablet.department_id, tablet.id))
    ).content.decode()

    assert "Move Tablet" not in html
    assert "Remove Tablet" not in html
    assert "Replace installation" not in html
    assert "Tablet Pending" not in html
    assert "Tablet Stale" not in html


def test_list_has_no_stale_or_pending_state(client, operational_tablet):
    user, tablet = operational_tablet
    client.force_login(user)
    html = client.get(reverse("tablet-list", args=(tablet.department_id,))).content.decode()
    assert "Pending" not in html
    # "Stale" may appear only as an installation-health filter (lowercase value),
    # never as a physical asset-state option.
    assert 'value="STALE"' not in html
