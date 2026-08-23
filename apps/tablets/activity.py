"""Best-effort, bounded diagnostics for authenticated Tablet API requests.

This module records safe metadata for one authenticated Tablet API request per
call. It is deliberately isolated from the provisioning/authorization lifecycle:
persistence here must never change Tablet API response semantics, block a valid
request, or leak secrets.
"""

import time

from django.utils import timezone

from apps.tablets.models import AppInstallation, TabletApiActivity

# Retention is bounded per installation and pruned by the
# ``prune_tablet_api_activity`` management command; see section on retention.
MAX_ACTIVITY_RECORDS_PER_INSTALLATION = 200
ACTIVITY_RETENTION_DAYS = 90

# A bounded cap on the path length is enforced both here and by the model field.
_MAX_PATH_LENGTH = 256
_MAX_METHOD_LENGTH = 8


def record_tablet_api_activity(request, response) -> None:
    """Persist safe metadata for one authenticated Tablet API request.

    Only requests with a resolved ``AppInstallation`` on ``request.user`` are
    recorded. Query strings, tokens, headers, bodies, and crypto material are
    never stored. Any persistence failure is swallowed so the request completes
    normally.
    """
    user = getattr(request, "user", None)
    installation = getattr(user, "installation", None)
    if not isinstance(installation, AppInstallation):
        return
    started = getattr(request, "_api_activity_started_at", None)
    duration_ms = None
    if started is not None:
        try:
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        except TypeError:
            duration_ms = None
    method = (request.method or "").upper()[:_MAX_METHOD_LENGTH]
    path = (request.path or "")[:_MAX_PATH_LENGTH]
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return
    try:
        TabletApiActivity.objects.create(
            app_installation=installation,
            occurred_at=timezone.now(),
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 - diagnostic persistence is best-effort by design.
        return


def prune_tablet_api_activity(*, now=None) -> int:
    """Delete activity outside the bounded retention window and per-installation cap.

    Runs as a housekeeping command rather than synchronously on every request so
    the hot Tablet API path never performs expensive cleanup work.
    """
    from datetime import timedelta

    now = now or timezone.now()
    deleted = 0
    deleted += TabletApiActivity.objects.filter(
        occurred_at__lt=now - timedelta(days=ACTIVITY_RETENTION_DAYS)
    ).delete()[0]
    installation_ids = TabletApiActivity.objects.values_list(
        "app_installation_id", flat=True
    ).distinct()
    for installation_id in installation_ids:
        keep = list(
            TabletApiActivity.objects.filter(app_installation_id=installation_id)
            .order_by("-occurred_at")
            .values_list("id", flat=True)[:MAX_ACTIVITY_RECORDS_PER_INSTALLATION]
        )
        deleted += (
            TabletApiActivity.objects.filter(app_installation_id=installation_id)
            .exclude(id__in=keep)
            .delete()[0]
        )
    return deleted
