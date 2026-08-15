import secrets
import time
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

PENDING_ACTION_MAX_AGE_SECONDS = 300


@dataclass(frozen=True)
class PendingAction:
    action_url: str
    action_method: str
    return_url: str


class ReauthRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


def _validate_local_return_url(return_url: str) -> str:
    """Return a browser-safe local continuation URL or reject it.

    Reauthentication continuations intentionally accept only relative local paths.
    They are generated with ``reverse()`` by protected views and must never become
    an open redirect or a mechanism for replaying the original request.
    """
    if (
        not isinstance(return_url, str)
        or not return_url.startswith("/")
        or return_url.startswith("//")
        or not url_has_allowed_host_and_scheme(return_url, allowed_hosts=set())
    ):
        raise ValueError("return_url must be a safe local URL.")
    return return_url


def require_recent_reauthentication(request, *, return_url: str) -> None:
    safe_return_url = _validate_local_return_url(return_url)
    timestamp = request.session.get("recent_reauthentication_at")
    if not timestamp or time.time() - timestamp > settings.RECENT_REAUTH_MAX_AGE_SECONDS:
        # Keep only a server-side continuation. The original POST data is never retained.
        token = secrets.token_urlsafe(32)
        pending = {
            "token": token,
            "user_id": str(request.user.id),
            "url": request.path,
            "method": request.method,
            "return_url": safe_return_url,
            "exp": int(time.time()) + PENDING_ACTION_MAX_AGE_SECONDS,
        }
        request.session["pending_reauth"] = pending
        raise ReauthRedirect(f"{reverse('accounts-reauthenticate')}?pending={token}")


def pending_action(request, token: str, *, consume: bool = False) -> PendingAction | None:
    pending = request.session.get("pending_reauth")
    if not isinstance(pending, dict):
        return None
    if (
        not secrets.compare_digest(str(pending.get("token", "")), token)
        or pending.get("user_id") != str(request.user.id)
        or pending.get("exp", 0) < int(time.time())
    ):
        return None
    try:
        action = PendingAction(
            action_url=pending["url"],
            action_method=pending["method"],
            return_url=_validate_local_return_url(pending["return_url"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if consume:
        del request.session["pending_reauth"]
    return action
