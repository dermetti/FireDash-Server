from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import AuthenticationThrottle


@pytest.mark.django_db
def test_housekeeping_preserves_current_windows_lockouts_and_sessions(settings):
    now = timezone.now()
    old = now - timedelta(days=8)

    def counter(name, window, lock=None, updated=old):
        row = AuthenticationThrottle.objects.create(
            scope="PASSWORD",
            subject="ACCOUNT",
            subject_hash=name,
            window_started_at=window,
            locked_until=lock,
        )
        AuthenticationThrottle.objects.filter(pk=row.pk).update(updated_at=updated)
        return row.pk

    obsolete = counter("obsolete", old)
    active = counter("active", now)
    locked = counter("locked", old, now + timedelta(days=1))
    recent = counter("recent", old, updated=now)
    Session.objects.create(session_key="expired", session_data="", expire_date=old)
    Session.objects.create(
        session_key="active", session_data="", expire_date=now + timedelta(days=1)
    )
    for _ in range(2):
        call_command("cleanup_authentication_throttles")
        call_command("clearsessions")
        assert not AuthenticationThrottle.objects.filter(pk=obsolete).exists()
        assert set(AuthenticationThrottle.objects.values_list("pk", flat=True)) == {
            active,
            locked,
            recent,
        }
        assert list(Session.objects.values_list("session_key", flat=True)) == ["active"]

    # An operator's longer effective throttle window is also retained.
    settings.AUTH_THROTTLE_WINDOW_SECONDS = 10 * 86400
    long_window = counter("long-window", old)
    call_command("cleanup_authentication_throttles")
    assert AuthenticationThrottle.objects.filter(pk=long_window).exists()
