import secrets
import time
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse

PENDING_ACTION_MAX_AGE_SECONDS = 300


@dataclass(frozen=True)
class PendingAction:
    url: str
    method: str


class ReauthRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


def require_recent_reauthentication(request) -> None:
    timestamp = request.session.get("recent_reauthentication_at")
    if not timestamp or time.time() - timestamp > settings.RECENT_REAUTH_MAX_AGE_SECONDS:
        # Keep only a server-side continuation. The original POST data is never retained.
        token = secrets.token_urlsafe(32)
        pending = {
            "token": token,
            "user_id": str(request.user.id),
            "url": request.path,
            "method": request.method,
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
    action = PendingAction(url=pending["url"], method=pending["method"])
    if consume:
        del request.session["pending_reauth"]
    return action
