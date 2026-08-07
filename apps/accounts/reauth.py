from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone


def require_recent_reauthentication(request) -> None:
    timestamp = request.session.get("recent_reauthentication_at")
    if (
        not timestamp
        or timezone.now().timestamp() - timestamp > settings.RECENT_REAUTH_MAX_AGE_SECONDS
    ):
        raise PermissionDenied("Recent reauthentication is required.")
