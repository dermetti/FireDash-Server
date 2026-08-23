"""Stage 2.1 stale-is-installation-health regression tests (PostgreSQL-backed)."""

from datetime import timedelta

from django.utils import timezone

from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import mark_stale_installations

from .conftest import _adopt  # noqa: E402


def test_stale_installation_does_not_make_tablet_stale(operational_tablet):
    user, tablet = operational_tablet
    installation, _ = _adopt(user, tablet)
    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))

    mark_stale_installations(now=timezone.now())

    installation.refresh_from_db()
    tablet.refresh_from_db()
    assert installation.status == AppInstallation.Status.STALE
    assert tablet.status == Tablet.Status.ACTIVE


def test_stale_health_derives_from_lease_expiry(operational_tablet):
    user, tablet = operational_tablet
    installation, _ = _adopt(user, tablet)
    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))

    assert mark_stale_installations(now=timezone.now()) == 1
    installation.refresh_from_db()
    assert installation.stale_at is not None
    assert installation.authorization_valid_until <= timezone.now()


def test_tablet_has_no_stale_state_constant():
    assert not hasattr(Tablet.Status, "STALE")
    assert not hasattr(Tablet.Status, "PENDING")
